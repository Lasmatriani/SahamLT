"""
data_source.py  –  IDX Official API
=====================================
Endpoint:
  1. GetStockSummary  → harga terkini (stabil ✅)
     URL: https://www.idx.co.id/primary/TradingSummary/GetStockSummary
          ?start=0&length=1&code={code}&lang=id

  2. GetChartStockbyCode → historis OHLCV (kadang 503, retry 3x)
     URL: https://www.idx.co.id/primary/StockData/GetChartStockbyCode
          ?indexCode={code}&period={period}

Field mapping GetStockSummary (dari response nyata IDX):
  StockCode, StockName, Previous, OpenPrice, High, Low, Close,
  Change, Volume, Date

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

    Response JSON structure (verified dari IDX):
      {
        "draw": 0,
        "recordsTotal": 1,
        "recordsFiltered": 1,
        "data": [
          {
            "StockCode":  "BBRI",
            "StockName":  "Bank Rakyat Indonesia (Persero) Tbk.",
            "Previous":   2900.0,
            "OpenPrice":  2900.0,
            "High":       2930.0,
            "Low":        2780.0,
            "Close":      2810.0,
            "Change":     -90.0,
            "Volume":     493450500.0,
            "Date":       "2026-06-04T00:00:00",
            ...
          }
        ]
      }

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

            # IDX response: key "data" (lowercase) berisi list
            rows = payload.get("data", payload.get("Data", []))

            if not rows:
                logger.warning(f"[IDX Summary] {code}: data kosong (recordsFiltered=0)")
                return None

            row = rows[0]

            # ── Field mapping sesuai response IDX nyata ──
            close   = float(row.get("Close")     or 0)
            prev    = float(row.get("Previous")  or close)
            open_   = float(row.get("OpenPrice") or prev)
            high    = float(row.get("High")      or close)
            low     = float(row.get("Low")       or close)
            vol     = int(float(row.get("Volume") or 0))
            chg     = float(row.get("Change")    or 0)

            # change_pct: hitung dari Change/Previous
            # (IDX tidak kasih langsung, tapi ada field "persen" yg null)
            chg_pct = (chg / prev * 100) if prev > 0 else 0.0

            # Date: "2026-06-04T00:00:00" → ambil 10 karakter pertama
            raw_date = row.get("Date") or ""
            date_str = raw_date[:10] if raw_date else ""

            result = {
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

            logger.info(
                f"[IDX Summary] ✅ {code}: Close={close:,.0f} "
                f"({chg:+.0f} / {chg_pct:+.2f}%) "
                f"Vol={vol:,} Date={date_str}"
            )
            return result

        except Exception as e:
            logger.warning(f"[IDX Summary] {code} attempt {attempt+1}/{MAX_RETRY}: {e}")
            if attempt < MAX_RETRY - 1:
                _sleep()

    logger.error(f"[IDX Summary] ❌ {code}: semua retry gagal")
    return None


# ── IDX GetChartStockbyCode (historis OHLCV) ─────────────────────────────
def get_stock_data(code: str, days: int = 200) -> Optional[pd.DataFrame]:
    """
    Ambil data historis OHLCV dari IDX GetChartStockbyCode.
    Retry 3x dengan jitter sleep untuk bypass Varnish 503.

    Period mapping:
      ≤90 hari  → period=90
      ≤180 hari → period=180
      ≤365 hari → period=365
      >365 hari → period=730

    Return: pd.DataFrame (DatetimeIndex, kolom Open/High/Low/Close/Volume)
            atau None jika gagal.
    """
    code   = code.upper().strip()
    period = 90 if days <= 90 else 180 if days <= 180 else 365 if days <= 365 else 730
    url    = IDX_CHART_URL.format(code=code, period=period)
    sess   = _session()

    for attempt in range(MAX_RETRY):
        try:
            r = sess.get(url, timeout=20)

            if r.status_code == 503:
                logger.warning(f"[IDX Chart] 503 Varnish attempt {attempt+1}/{MAX_RETRY} — {code}")
                _sleep(3.0)
                continue

            r.raise_for_status()

            raw  = r.json()

            # IDX chart bisa return: {"data": [...]} atau {"Data": [...]} atau langsung list
            if isinstance(raw, list):
                rows = raw
            else:
                rows = raw.get("data", raw.get("Data", []))

            if not rows:
                logger.warning(f"[IDX Chart] {code}: response kosong (attempt {attempt+1})")
                _sleep()
                continue

            df = pd.DataFrame(rows)
            logger.debug(f"[IDX Chart] {code}: kolom raw = {list(df.columns)}")

            # ── Normalize nama kolom (IDX tidak konsisten) ──
            rename_map = {}
            col_lower  = {c.lower(): c for c in df.columns}

            for target, candidates in {
                "Open":   ["openprice", "open_price", "open"],
                "High":   ["high"],
                "Low":    ["low"],
                "Close":  ["close"],
                "Volume": ["volume"],
                "Date":   ["date"],
            }.items():
                for cand in candidates:
                    if cand in col_lower:
                        rename_map[col_lower[cand]] = target
                        break

            df = df.rename(columns=rename_map)

            # Pastikan kolom wajib ada
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col not in df.columns:
                    df[col] = 0.0

            # ── Parse Date sebagai index ──
            if "Date" in df.columns:
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                df = df.dropna(subset=["Date"]).set_index("Date")
            else:
                df.index = pd.to_datetime(df.index, errors="coerce")

            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]

            # Convert ke numerik
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["Close"])
            df = df[df["Close"] > 0]   # buang baris Close=0
            df = df[["Open", "High", "Low", "Close", "Volume"]]

            if len(df) < 10:
                logger.warning(f"[IDX Chart] {code}: data terlalu sedikit ({len(df)} baris, attempt {attempt+1})")
                _sleep()
                continue

            logger.info(f"[IDX Chart] ✅ {code}: {len(df)} baris | {df.index[0].date()} → {df.index[-1].date()}")
            return df

        except Exception as e:
            logger.warning(f"[IDX Chart] {code} attempt {attempt+1}/{MAX_RETRY}: {e}")
            if attempt < MAX_RETRY - 1:
                _sleep(2.0)

    logger.error(f"[IDX Chart] ❌ {code}: semua {MAX_RETRY} retry gagal")
    return None


# ── Diagnostik /testsource ───────────────────────────────────────────────
def test_sources(code: str = "BBRI") -> dict:
    """
    Uji kedua endpoint IDX dan return dict hasil.
    Dipanggil dari /testsource di bot.
    """
    results = {}

    # Test 1: IDX Summary (harga terkini)
    try:
        s = get_current_price(code)
        if s and s["close"] > 0:
            results["idx_summary"] = (
                f"✅ {s['name'][:25]} | "
                f"Close=Rp {s['close']:,.0f} ({s['change']:+.0f}) | "
                f"Vol={s['volume']:,} | {s['date']}"
            )
        elif s:
            results["idx_summary"] = f"⚠️ Data ada tapi Close=0 (suspended/no-trade)"
        else:
            results["idx_summary"] = "❌ Kode tidak ditemukan atau API error"
    except Exception as e:
        results["idx_summary"] = f"❌ Exception: {e}"

    # Test 2: IDX Chart (data historis)
    try:
        df = get_stock_data(code, 60)
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            results["idx_chart"] = (
                f"✅ {len(df)} baris | "
                f"Last: {df.index[-1].date()} | "
                f"Close={last['Close']:,.0f}"
            )
        else:
            results["idx_chart"] = "❌ Gagal / 503 / data kosong"
    except Exception as e:
        results["idx_chart"] = f"❌ Exception: {e}"

    return results
