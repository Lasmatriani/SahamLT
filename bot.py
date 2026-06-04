import os
import json
import asyncio
import logging
from datetime import datetime
import pytz
import pandas as pd
import numpy as np
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from data_source import get_current_price, get_stock_data, test_sources

# ── Config ──────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.environ["BOT_TOKEN"]
ALLOWED_IDS    = set(map(int, os.environ.get("ALLOWED_USER_IDS","").split(","))) if os.environ.get("ALLOWED_USER_IDS") else set()
WIB            = pytz.timezone("Asia/Jakarta")
DEFAULT_CL_PCT = float(os.environ.get("DEFAULT_CL_PCT", "-7"))
DEFAULT_TP_PCT = float(os.environ.get("DEFAULT_TP_PCT", "15"))

# ── Storage ─────────────────────────────────────────────────────────────
_VOLUME_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/data")
_LOCAL_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def _get_data_dir() -> str:
    if os.path.isdir(_VOLUME_DIR):
        return _VOLUME_DIR
    os.makedirs(_LOCAL_DIR, exist_ok=True)
    return _LOCAL_DIR

def _data_path(fn): return os.path.join(_get_data_dir(), fn)

def load_watchlist() -> dict:
    for path in [_data_path("watchlist.json"), _data_path("watchlist.json.bak")]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Load gagal {path}: {e}")
    return {}

def save_watchlist(wl: dict):
    import shutil
    primary, tmp, backup = _data_path("watchlist.json"), _data_path("watchlist.json.tmp"), _data_path("watchlist.json.bak")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(wl, f, indent=2, ensure_ascii=False)
    os.replace(tmp, primary)
    shutil.copy2(primary, backup)

def get_storage_info() -> dict:
    primary = _data_path("watchlist.json")
    mtime   = "–"
    if os.path.exists(primary):
        import datetime as dt
        mtime = dt.datetime.fromtimestamp(os.path.getmtime(primary), WIB).strftime("%d %b %Y %H:%M WIB")
    return {
        "mode":        "Railway Volume 💾" if os.path.isdir(_VOLUME_DIR) else "Local Storage 📁",
        "path":        _get_data_dir(),
        "has_primary": os.path.exists(primary),
        "has_backup":  os.path.exists(_data_path("watchlist.json.bak")),
        "last_saved":  mtime,
        "size":        os.path.getsize(primary) if os.path.exists(primary) else 0,
    }

# ── Data & Indikator ─────────────────────────────────────────────────────
def get_price_data(kode: str) -> dict | None:
    kode = kode.upper().strip()

    hist = get_stock_data(kode, days=200)
    if hist is None or len(hist) < 20:
        logger.warning(f"[get_price_data] data historis tidak cukup: {kode}")
        return None

    price_info = get_current_price(kode)
    if price_info and price_info["close"] > 0:
        current = price_info["close"]
        prev    = price_info["previous"]
        chg_pct = price_info["change_pct"]
        name    = price_info.get("name", kode)
    else:
        current = float(hist["Close"].iloc[-1])
        prev    = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
        chg_pct = ((current - prev) / prev * 100) if prev > 0 else 0.0
        name    = kode

    return _calculate_indicators(kode, name, current, prev, chg_pct, hist, None, None)


def _calculate_indicators(kode, name, current, prev, chg_pct, hist, pbv, per) -> dict:
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
    ema12          = close.ewm(span=12).mean()
    ema26          = close.ewm(span=26).mean()
    macd_line      = ema12 - ema26
    signal_line    = macd_line.ewm(span=9).mean()
    macd_hist      = float(macd_line.iloc[-1] - signal_line.iloc[-1])
    macd_hist_prev = float(macd_line.iloc[-2] - signal_line.iloc[-2])
    macd_val       = float(macd_line.iloc[-1])
    signal_val     = float(signal_line.iloc[-1])
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
    bb_mid_s  = close.rolling(20).mean()
    bb_std    = close.rolling(20).std()
    bb_upper  = float((bb_mid_s + 2 * bb_std).iloc[-1])
    bb_lower  = float((bb_mid_s - 2 * bb_std).iloc[-1])
    bb_mid_v  = float(bb_mid_s.iloc[-1])
    bb_width  = (bb_upper - bb_lower) / bb_mid_v if bb_mid_v > 0 else 0
    bb_pct    = (current - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
    bb_squeeze= bb_width < 0.10

    # Candlestick
    def body(i):    return abs(float(close.iloc[i]) - float(op.iloc[i]))
    def candle(i):  return float(hi.iloc[i]) - float(lo.iloc[i])
    def is_bull(i): return float(close.iloc[i]) > float(op.iloc[i])
    def is_bear(i): return float(close.iloc[i]) < float(op.iloc[i])

    patterns = []
    if candle(-1) > 0 and body(-1) / candle(-1) < 0.10:
        patterns.append(("DOJI", "neutral", "Ketidakpastian — tunggu konfirmasi arah ⚖️"))
    if candle(-1) > 0:
        ls = float(op.iloc[-1] if is_bull(-1) else close.iloc[-1]) - float(lo.iloc[-1])
        us = float(hi.iloc[-1]) - float(close.iloc[-1] if is_bull(-1) else op.iloc[-1])
        b  = body(-1)
        if ls > 2*b and us < b and b > 0:
            patterns.append(("HAMMER", "bullish", "Hammer — sinyal reversal bullish 🔨"))
        if us > 2*b and ls < b and b > 0:
            patterns.append(("SHOOTING STAR", "bearish", "Shooting Star — potensi reversal turun ⭐"))
    if len(close) >= 2:
        if is_bear(-2) and is_bull(-1) and body(-1) > body(-2):
            if float(close.iloc[-1]) > float(op.iloc[-2]) and float(op.iloc[-1]) < float(close.iloc[-2]):
                patterns.append(("BULLISH ENGULFING", "bullish", "Bullish Engulfing — sinyal beli kuat 🟢"))
        if is_bull(-2) and is_bear(-1) and body(-1) > body(-2):
            if float(close.iloc[-1]) < float(op.iloc[-2]) and float(op.iloc[-1]) > float(close.iloc[-2]):
                patterns.append(("BEARISH ENGULFING", "bearish", "Bearish Engulfing — sinyal jual kuat 🔴"))
    if len(close) >= 3:
        if is_bear(-3) and body(-3)>candle(-3)*0.6 and body(-2)<candle(-2)*0.3 and is_bull(-1) and body(-1)>candle(-1)*0.6:
            patterns.append(("MORNING STAR", "bullish", "Morning Star — reversal bullish kuat ⭐🌅"))
        if is_bull(-3) and body(-3)>candle(-3)*0.6 and body(-2)<candle(-2)*0.3 and is_bear(-1) and body(-1)>candle(-1)*0.6:
            patterns.append(("EVENING STAR", "bearish", "Evening Star — reversal bearish 🌆"))
        if all(is_bull(-i) for i in [1,2,3]) and float(close.iloc[-1])>float(close.iloc[-2])>float(close.iloc[-3]):
            patterns.append(("THREE WHITE SOLDIERS", "bullish", "3 White Soldiers — tren naik kuat 💪"))
        if all(is_bear(-i) for i in [1,2,3]) and float(close.iloc[-1])<float(close.iloc[-2])<float(close.iloc[-3]):
            patterns.append(("THREE BLACK CROWS", "bearish", "3 Black Crows — tren turun kuat 🐦"))

    bull = sum(1 for p in patterns if p[1]=="bullish")
    bear = sum(1 for p in patterns if p[1]=="bearish")
    candle_bias = "bullish" if bull>bear else ("bearish" if bear>bull else "neutral")

    return {
        "kode": kode, "name": name, "current": current, "prev": prev, "chg_pct": chg_pct,
        "rsi": rsi, "macd": macd_val, "signal": signal_val,
        "macd_hist": macd_hist, "macd_hist_prev": macd_hist_prev, "macd_golden_cross": macd_golden_cross,
        "ma20": ma20, "ma50": ma50, "support": support, "resistance": resistance,
        "vol_ratio": vol_ratio,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_mid": bb_mid_v,
        "bb_pct": bb_pct, "bb_width": bb_width, "bb_squeeze": bb_squeeze,
        "patterns": patterns, "candle_bias": candle_bias,
        "pbv": pbv, "per": per,
        "hist": hist.tail(60),
    }


def analyze(d: dict, entry: float, cl_pct: float, tp_pct: float) -> dict:
    price      = d["current"]
    rsi        = d["rsi"]
    mh         = d["macd_hist"]
    gc         = d["macd_golden_cross"]
    support    = d["support"]
    resist     = d["resistance"]
    ma20       = d["ma20"]
    vol_ratio  = d["vol_ratio"]
    pbv        = d["pbv"]
    per        = d["per"]
    bb_upper   = d["bb_upper"]
    bb_lower   = d["bb_lower"]
    bb_pct     = d["bb_pct"]
    bb_squeeze = d["bb_squeeze"]
    patterns   = d["patterns"]
    candle_bias= d["candle_bias"]

    cl_final = max(support * 0.99, entry * (1 + cl_pct / 100))
    tp_final = min(resist,         entry * (1 + tp_pct / 100))
    cl_tech  = support * 0.99
    tp_tech  = resist

    signals, conditions, tech_met = [], [], 0

    if rsi < 30:
        signals.append(f"RSI oversold {rsi:.1f} — potensi reversal kuat 🟢"); conditions.append(True);  tech_met += 1
    elif rsi < 50:
        signals.append(f"RSI {rsi:.1f} — momentum belum overbought 🟢");      conditions.append(True);  tech_met += 1
    elif rsi > 70:
        signals.append(f"RSI overbought {rsi:.1f} — hati-hati koreksi 🔴");   conditions.append(False)
    else:
        signals.append(f"RSI {rsi:.1f} — zona netral ⚖️");                    conditions.append(False)

    if gc:
        signals.append("MACD golden cross — baru balik bullish 🚀");           conditions.append(True);  tech_met += 1
    elif mh > 0:
        signals.append(f"MACD histogram positif ({mh:+.2f}) 🟢");             conditions.append(True);  tech_met += 1
    else:
        signals.append(f"MACD histogram negatif ({mh:+.2f}) 🔴");             conditions.append(False)

    if price > ma20:
        signals.append(f"Harga di atas MA20 ({fmt(ma20)}) 🟢");               conditions.append(True);  tech_met += 1
    else:
        signals.append(f"Harga di bawah MA20 — gap {((ma20-price)/ma20*100):.1f}% 🔴"); conditions.append(False)

    if price > support:
        signals.append(f"Di atas support Rp {fmt(support)} 🟢");              conditions.append(True)
    else:
        signals.append(f"Di bawah support Rp {fmt(support)} — waspada 🔴");   conditions.append(False)

    if vol_ratio >= 1.5:
        signals.append(f"Volume spike {vol_ratio:.1f}x — ada minat beli 🔥"); conditions.append(True)
    elif vol_ratio < 0.5:
        signals.append(f"Volume sangat sepi {vol_ratio:.1f}x — sinyal lemah 😴"); conditions.append(False)
    else:
        signals.append(f"Volume normal {vol_ratio:.1f}x ⚖️");                 conditions.append(False)

    if bb_squeeze:
        signals.append("BB Squeeze — volatilitas rendah, potensi breakout ⚡"); conditions.append(True)
    elif bb_pct < 0.20:
        signals.append(f"Harga dekat Lower BB ({fmt(bb_lower)}) — potensi rebound 🟢"); conditions.append(True)
    elif bb_pct > 0.90:
        signals.append(f"Harga dekat Upper BB ({fmt(bb_upper)}) — hati-hati overbought 🔴"); conditions.append(False)
    else:
        signals.append(f"BB normal — posisi {int(bb_pct*100)}% dalam band ⚖️"); conditions.append(False)

    if patterns:
        for p in patterns: signals.append(f"Candle: {p[2]}")
        conditions.append(candle_bias == "bullish")
    else:
        signals.append("Tidak ada pola candlestick signifikan ⚖️"); conditions.append(False)

    fund_ok = False
    if pbv is not None and per is not None:
        if pbv < 2.0 and 0 < per < 25:
            signals.append(f"Fundamental menarik: PBV {pbv:.2f}x, PER {per:.1f}x 🟢"); conditions.append(True); fund_ok = True
        elif pbv > 4.0 or per > 40:
            signals.append(f"Valuasi mahal: PBV {pbv:.2f}x, PER {per:.1f}x 🔴"); conditions.append(False)
        else:
            signals.append(f"Fundamental netral: PBV {pbv:.2f}x, PER {per:.1f}x ⚖️"); conditions.append(False)
    elif pbv is not None:
        if pbv < 1.0:
            signals.append(f"PBV sangat murah {pbv:.2f}x 🟢"); conditions.append(True); fund_ok = True
        else:
            signals.append(f"PBV {pbv:.2f}x ⚖️"); conditions.append(False)
    else:
        signals.append("Data fundamental tidak tersedia ⚖️"); conditions.append(False)

    met       = sum(conditions)
    rugi      = (per is not None and per < 0)
    terlambat = price > resist * 1.10
    if rugi:      signals.append("⛔ Perusahaan RUGI — override ke Sell")
    if terlambat: signals.append("⚠️ Harga sudah jauh di atas resistance — terlambat masuk")

    if rugi or terlambat:
        verdict = "STRONG SELL ⚠️" if (met <= 2 or rugi) else "SELL 📉"
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

    alert = None
    if price <= cl_final:   alert = "CUT_LOSS"
    elif price >= tp_final: alert = "TAKE_PROFIT"

    return {
        "score": met, "tech_met": tech_met, "verdict": verdict,
        "signals": signals, "conditions": conditions,
        "cl": cl_final, "tp": tp_final, "cl_tech": cl_tech, "tp_tech": tp_tech,
        "pnl_pct": ((price - entry) / entry * 100) if entry > 0 else 0,
        "alert": alert, "rugi": rugi, "terlambat": terlambat, "fund_ok": fund_ok,
    }

# ── Helpers ──────────────────────────────────────────────────────────────
def fmt(n, dec=0):
    if n is None: return "–"
    return f"{n:,.{dec}f}"

def pct_emoji(v):
    if v > 2: return "🟢"
    if v > 0: return "🔼"
    if v < -2: return "🔴"
    return "🔽"

def build_card(d: dict, ana: dict, entry: float) -> str:
    chg   = d["chg_pct"]
    pnl   = ana["pnl_pct"]
    score = ana["score"]
    tech_met = ana["tech_met"]

    alert_line = ""
    if ana["alert"] == "CUT_LOSS":
        alert_line = "\n\n🚨 *ALERT: HARGA MENYENTUH CUT LOSS!*\nSegera evaluasi posisi kamu."
    elif ana["alert"] == "TAKE_PROFIT":
        alert_line = "\n\n🎯 *ALERT: TARGET TAKE PROFIT TERCAPAI!*\nPertimbangkan untuk realisasi profit."

    rsi_sig  = next((s for s in ana["signals"] if "RSI" in s), "")
    macd_sig = next((s for s in ana["signals"] if "MACD" in s), "")
    ma_sig   = next((s for s in ana["signals"] if "MA20" in s or ("MA" in s and "MACD" not in s)), "")
    sup_sig  = next((s for s in ana["signals"] if "support" in s.lower()), "")
    vol_sig  = next((s for s in ana["signals"] if "Volume" in s or "volume" in s), "")
    bb_sig   = next((s for s in ana["signals"] if "BB" in s), "")
    candle_sigs   = [s for s in ana["signals"] if "Candle:" in s]
    fund_sig      = next((s for s in ana["signals"] if any(k in s for k in ["Fundamental","PBV","Valuasi","murah"])), "")
    override_sigs = [s for s in ana["signals"] if any(k in s for k in ["RUGI","terlambat","⛔","⚠️"])]

    pbv_txt = f"{d['pbv']:.2f}x" if d['pbv'] else "–"
    per_txt = f"{d['per']:.1f}x" if d['per'] else "–"

    bb_pos = max(0, min(10, int(d["bb_pct"] * 10)))
    bb_bar = "░"*bb_pos + "▓" + "░"*(10-bb_pos)
    bb_squeeze_tag = " ⚡SQUEEZE" if d["bb_squeeze"] else ""

    candle_txt   = "\n".join(f"  {s.replace('Candle: ','')}" for s in candle_sigs) if candle_sigs else "  Tidak ada pola signifikan"
    override_txt = ("\n" + "\n".join(f"  {s}" for s in override_sigs)) if override_sigs else ""

    filled = "█" * score
    empty  = "░" * (8 - score)
    bar    = f"{filled}{empty} {score}/8"

    v = ana["verdict"]
    if "STRONG BUY" in v:    reason = f"6+ kondisi ✅ · {tech_met} teknikal inti · fundamental ✅"
    elif "BUY" in v:         reason = f"5+ kondisi ✅ · {tech_met} teknikal inti terpenuhi"
    elif "NEUTRAL" in v:     reason = "Sinyal campuran — tunggu konfirmasi lebih lanjut"
    elif "STRONG SELL" in v: reason = "Terlalu sedikit kondisi" + (" · Perusahaan RUGI" if ana.get("rugi") else "")
    else:                    reason = "Kurang dari 3 kondisi terpenuhi"
    if ana.get("terlambat"): reason += " · Harga terlalu jauh dari resistance"

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *{d['kode']}* — {d['name'][:28]}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Harga: *Rp {fmt(d['current'])}* {pct_emoji(chg)} {chg:+.2f}%\n"
        f"📥 Entry: Rp {fmt(entry)} | {'🟢' if pnl>=0 else '🔴'} P/L: *{pnl:+.1f}%*\n\n"
        f"📊 *Teknikal:*\n"
        f"  {rsi_sig}\n  {macd_sig}\n  {ma_sig}\n  {sup_sig}\n  {vol_sig}\n\n"
        f"📉 *Bollinger Bands:*\n"
        f"  Upper: {fmt(d['bb_upper'])} | Mid: {fmt(d['bb_mid'])} | Lower: {fmt(d['bb_lower'])}\n"
        f"  Posisi: `[{bb_bar}]`{bb_squeeze_tag}\n"
        f"  {bb_sig}\n\n"
        f"🕯 *Candlestick Pattern:*\n{candle_txt}\n\n"
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

# ── Chart ─────────────────────────────────────────────────────────────────
def generate_chart(d: dict, ana: dict, entry: float) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.gridspec import GridSpec
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
    rsi_s    = 100 - (100 / (1 + gain / loss))

    BG="$0f1520"; SURFACE="#151e2e"; GREEN="#00e5a0"; RED="#ff4560"
    BLUE="#0099ff"; YELLOW="#ffa500"; MUTED="#5a7090"; TEXT="#e2eaf5"
    BG="#0f1520"

    fig = plt.figure(figsize=(12, 9), facecolor=BG)
    gs  = GridSpec(4, 1, figure=fig, height_ratios=[4,1.2,1,1], hspace=0.06)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3], sharex=ax1)

    for ax in [ax1,ax2,ax3,ax4]:
        ax.set_facecolor(SURFACE)
        ax.tick_params(colors=MUTED, labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1e2d42")

    n  = len(dates)
    xs = range(n)

    for i,(o,h,l,c) in enumerate(zip(open_,high,low,close)):
        color = GREEN if c>=o else RED
        ax1.bar(i, abs(c-o), bottom=min(c,o), color=color, width=0.72, alpha=0.9, zorder=3)
        ax1.plot([i,i],[l,h], color=color, linewidth=0.8, zorder=2)

    ax1.fill_between(xs, bb_lo_s, bb_up_s, alpha=0.08, color=BLUE, zorder=1)
    ax1.plot(xs, bb_up_s, color=BLUE,     linewidth=0.7, alpha=0.5, linestyle="--", label="BB")
    ax1.plot(xs, bb_lo_s, color=BLUE,     linewidth=0.7, alpha=0.5, linestyle="--")
    ax1.plot(xs, ma20_s,  color=YELLOW,   linewidth=1.0, label="MA20", zorder=4)
    ax1.plot(xs, ma50_s,  color="#ff6b9d",linewidth=1.0, label="MA50", zorder=4)
    ax1.axhline(ana["cl"],  color=RED,    linewidth=1.2, linestyle="--", alpha=0.8)
    ax1.axhline(ana["tp"],  color=GREEN,  linewidth=1.2, linestyle="--", alpha=0.8)
    ax1.axhline(entry,      color=YELLOW, linewidth=0.9, linestyle=":",  alpha=0.7)
    ax1.text(n-0.5, ana["cl"], f" CL {ana['cl']:,.0f}",  color=RED,    fontsize=7, va="center")
    ax1.text(n-0.5, ana["tp"], f" TP {ana['tp']:,.0f}",  color=GREEN,  fontsize=7, va="center")
    ax1.text(n-0.5, entry,     f" Entry {entry:,.0f}",   color=YELLOW, fontsize=7, va="center")

    for pat in d["patterns"]:
        color  = GREEN if pat[1]=="bullish" else (RED if pat[1]=="bearish" else YELLOW)
        marker = "^" if pat[1]=="bullish" else ("v" if pat[1]=="bearish" else "D")
        ypos   = float(low.iloc[-1])*0.995 if pat[1]!="bearish" else float(high.iloc[-1])*1.005
        ax1.scatter(n-1, ypos, color=color, marker=marker, s=80, zorder=6)
        ax1.annotate(pat[0],(n-1,ypos),textcoords="offset points",
                     xytext=(0,-12 if pat[1]=="bearish" else 8),
                     fontsize=6, color=color, ha="center")

    ax1.set_title(
        f"{d['kode']} — {d['name'][:35]}   |   "
        f"Rp {d['current']:,.0f}  {d['chg_pct']:+.2f}%   |   "
        f"RSI {d['rsi']:.1f}   |   {ana['verdict']}",
        color=TEXT, fontsize=9, pad=8, loc="left"
    )
    ax1.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")
    ax1.set_ylabel("Harga (IDR)", color=MUTED, fontsize=7)

    avg_vol    = volume.rolling(10).mean()
    vol_colors = [GREEN if c>=o else RED for c,o in zip(close,open_)]
    ax2.bar(xs, volume/1e6, color=vol_colors, alpha=0.7, width=0.8)
    ax2.plot(xs, avg_vol/1e6, color=YELLOW, linewidth=0.8, label="Vol MA10")
    ax2.set_ylabel("Vol (M)", color=MUTED, fontsize=7)
    ax2.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")

    ax3.plot(xs, rsi_s, color=BLUE, linewidth=1.0)
    ax3.axhline(70, color=RED,   linewidth=0.6, linestyle="--", alpha=0.6)
    ax3.axhline(30, color=GREEN, linewidth=0.6, linestyle="--", alpha=0.6)
    ax3.axhline(50, color=MUTED, linewidth=0.4, linestyle=":",  alpha=0.4)
    ax3.fill_between(xs, rsi_s, 70, where=(rsi_s>=70), alpha=0.15, color=RED)
    ax3.fill_between(xs, rsi_s, 30, where=(rsi_s<=30), alpha=0.15, color=GREEN)
    ax3.set_ylim(0,100)
    ax3.set_ylabel("RSI", color=MUTED, fontsize=7)
    ax3.text(n-1, float(rsi_s.iloc[-1]), f" {float(rsi_s.iloc[-1]):.1f}", color=BLUE, fontsize=7, va="center")

    mh_colors = [GREEN if v>=0 else RED for v in mh_s]
    ax4.bar(xs, mh_s, color=mh_colors, alpha=0.8, width=0.8)
    ax4.plot(xs, ml, color=BLUE,   linewidth=0.8, label="MACD")
    ax4.plot(xs, sl, color=YELLOW, linewidth=0.8, label="Signal")
    ax4.axhline(0, color=MUTED, linewidth=0.5, alpha=0.5)
    ax4.set_ylabel("MACD", color=MUTED, fontsize=7)
    ax4.legend(fontsize=6, facecolor=BG, edgecolor=MUTED, labelcolor=MUTED, loc="upper left")

    tick_step = max(1, n//8)
    ax4.set_xticks(range(0,n,tick_step))
    ax4.set_xticklabels([dates[i].strftime("%d/%m") for i in range(0,n,tick_step)], color=MUTED, fontsize=7)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    plt.setp(ax3.get_xticklabels(), visible=False)

    fig.text(0.99, 0.01, f"IDX Saham Bot · {datetime.now(WIB).strftime('%d %b %Y %H:%M WIB')} · Sumber: IDX Official",
             ha="right", color=MUTED, fontsize=6)
    plt.tight_layout()

    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ── Auth ──────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS: return True
    return update.effective_user.id in ALLOWED_IDS

# ── Handlers ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await update.message.reply_text(
        "👋 *Selamat datang di IDX Saham Bot!*\n\n"
        "Perintah:\n"
        "*/add KODE ENTRY* — tambah ke watchlist\n"
        "*/del KODE* — hapus saham\n"
        "*/list* — lihat watchlist\n"
        "*/cek KODE* — analisis + chart\n"
        "*/chart KODE* — chart saja\n"
        "*/setcl KODE %* — set cut loss (contoh: /setcl BBRI -8)\n"
        "*/settp KODE %* — set take profit (contoh: /settp BBRI 20)\n"
        "*/scan* — scan semua watchlist\n"
        "*/status* — status bot\n"
        "*/testsource KODE* — cek koneksi IDX API",
        parse_mode="Markdown"
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Format: /add KODE HARGA_ENTRY\nContoh: /add BBRI 3000")
        return
    kode = ctx.args[0].upper().strip()
    try:
        entry = float(ctx.args[1].replace(",",""))
    except ValueError:
        await update.message.reply_text("❌ Harga entry tidak valid.")
        return
    wl = load_watchlist()
    wl[kode] = {"entry": entry, "cl_pct": DEFAULT_CL_PCT, "tp_pct": DEFAULT_TP_PCT,
                "added": datetime.now(WIB).strftime("%Y-%m-%d %H:%M")}
    save_watchlist(wl)
    await update.message.reply_text(
        f"✅ *{kode}* ditambahkan!\nEntry: Rp {fmt(entry)}\n"
        f"CL: {DEFAULT_CL_PCT}% | TP: +{DEFAULT_TP_PCT}%",
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
    await update.message.reply_text(f"🗑 *{kode}* dihapus.", parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("📋 Watchlist kosong. Tambah dengan /add KODE ENTRY")
        return
    lines = ["📋 *Watchlist kamu:*\n"]
    for kode, meta in wl.items():
        lines.append(f"• *{kode}* — Entry: Rp {fmt(meta['entry'])} | CL: {meta['cl_pct']}% | TP: +{meta['tp_pct']}%")
    lines.append(f"\nTotal: {len(wl)} saham")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cek(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("❌ Format: /cek KODE\nContoh: /cek BBRI")
        return
    kode = ctx.args[0].upper().strip()
    wl   = load_watchlist()
    meta = wl.get(kode)
    msg  = await update.message.reply_text(f"⏳ Mengambil data {kode} dari IDX...")

    d = get_price_data(kode)
    if not d:
        await msg.edit_text(
            f"❌ Data *{kode}* tidak tersedia dari IDX.\n"
            "Kemungkinan:\n• Kode saham salah\n• IDX API sedang tidak stabil (coba lagi beberapa menit)\n"
            "Gunakan /testsource untuk cek status API.",
            parse_mode="Markdown"
        )
        return

    entry  = meta["entry"]  if meta else d["current"]
    cl_pct = meta["cl_pct"] if meta else DEFAULT_CL_PCT
    tp_pct = meta["tp_pct"] if meta else DEFAULT_TP_PCT

    ana = analyze(d, entry, cl_pct, tp_pct)
    d["verdict"] = ana["verdict"]
    note = "" if meta else "\n\n_💡 Saham belum di watchlist. Gunakan /add untuk pantau otomatis._"
    await msg.edit_text(build_card(d, ana, entry) + note, parse_mode="Markdown")

    await update.message.reply_text("📊 Membuat chart...")
    try:
        chart_bytes = await asyncio.get_event_loop().run_in_executor(None, generate_chart, d, ana, entry)
        from io import BytesIO
        await update.message.reply_photo(
            photo=BytesIO(chart_bytes),
            caption=f"📈 *{kode}* — 60 hari terakhir | Sumber: IDX Official",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Chart error {kode}: {e}")
        await update.message.reply_text("⚠️ Chart gagal dibuat. Data teks di atas tetap valid.")

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not ctx.args:
        await update.message.reply_text("❌ Format: /chart KODE\nContoh: /chart BBRI")
        return
    kode = ctx.args[0].upper().strip()
    wl   = load_watchlist()
    meta = wl.get(kode)
    msg  = await update.message.reply_text(f"📊 Membuat chart {kode}...")

    d = get_price_data(kode)
    if not d:
        await msg.edit_text(f"❌ Data *{kode}* tidak tersedia dari IDX. Coba lagi beberapa menit.", parse_mode="Markdown")
        return

    entry  = meta["entry"]  if meta else d["current"]
    cl_pct = meta["cl_pct"] if meta else DEFAULT_CL_PCT
    tp_pct = meta["tp_pct"] if meta else DEFAULT_TP_PCT
    ana    = analyze(d, entry, cl_pct, tp_pct)
    d["verdict"] = ana["verdict"]

    try:
        chart_bytes = await asyncio.get_event_loop().run_in_executor(None, generate_chart, d, ana, entry)
        from io import BytesIO
        await msg.delete()
        await update.message.reply_photo(
            photo=BytesIO(chart_bytes),
            caption=(
                f"📈 *{kode}* — {d['name'][:30]}\n"
                f"Rp {fmt(d['current'])}  {d['chg_pct']:+.2f}%  |  RSI {d['rsi']:.1f}  |  {ana['verdict']}\n"
                f"TP: {fmt(ana['tp'])}  •  CL: {fmt(ana['cl'])}"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Chart error {kode}: {e}")
        await msg.edit_text("⚠️ Gagal membuat chart.")

async def cmd_setcl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Format: /setcl KODE PERSEN\nContoh: /setcl BBRI -8")
        return
    kode = ctx.args[0].upper()
    try:    val = float(ctx.args[1])
    except: await update.message.reply_text("❌ % tidak valid."); return
    if val > 0: val = -val
    wl = load_watchlist()
    if kode not in wl:
        await update.message.reply_text(f"❌ {kode} tidak ada di watchlist."); return
    wl[kode]["cl_pct"] = val
    save_watchlist(wl)
    await update.message.reply_text(f"✅ CL *{kode}* → *{val}%*", parse_mode="Markdown")

async def cmd_settp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if len(ctx.args) < 2:
        await update.message.reply_text("❌ Format: /settp KODE PERSEN\nContoh: /settp BBRI 20")
        return
    kode = ctx.args[0].upper()
    try:    val = float(ctx.args[1])
    except: await update.message.reply_text("❌ % tidak valid."); return
    if val < 0: val = -val
    wl = load_watchlist()
    if kode not in wl:
        await update.message.reply_text(f"❌ {kode} tidak ada di watchlist."); return
    wl[kode]["tp_pct"] = val
    save_watchlist(wl)
    await update.message.reply_text(f"✅ TP *{kode}* → *+{val}%*", parse_mode="Markdown")

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("📋 Watchlist kosong.")
        return
    msg = await update.message.reply_text(f"⏳ Scanning {len(wl)} saham dari IDX...")
    await do_scan(ctx.bot, update.effective_chat.id, wl, header="📡 *SCAN MANUAL*")
    await msg.delete()

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        f"\n🔌 *Data Source:* IDX Official API",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_testsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    code = ctx.args[0].upper() if ctx.args else "BBRI"
    msg  = await update.message.reply_text(f"🔧 Menguji IDX API untuk *{code}*...", parse_mode="Markdown")

    results = test_sources(code)
    lines   = [f"🔌 *Test IDX API: {code}*", "━━━━━━━━━━━━━━━"]
    labels  = {"idx_summary": "IDX Summary (harga terkini)", "idx_chart": "IDX Chart (data historis)"}
    for key, label in labels.items():
        lines.append(f"• *{label}*: `{results.get(key,'?')}`")
    lines += ["", "_Summary → /cek_", "_Chart → /chart, /scan, indikator_"]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await cmd_start(update, ctx)

# ── Scan logic ────────────────────────────────────────────────────────────
async def do_scan(bot, chat_id: int, wl: dict, header: str = ""):
    now_str = datetime.now(WIB).strftime("%d %b %Y %H:%M WIB")
    alerts, results = [], []

    for kode, meta in wl.items():
        d = get_price_data(kode)
        if not d:
            continue
        ana = analyze(d, meta["entry"], meta["cl_pct"], meta["tp_pct"])
        results.append((d, ana, meta["entry"]))
        if ana["alert"]:
            alerts.append((kode, ana["alert"], d["current"], ana["cl"], ana["tp"]))

    if not results:
        await bot.send_message(chat_id, "⚠️ Gagal mengambil data dari IDX. Coba lagi nanti.")
        return

    await bot.send_message(chat_id, f"{header}\n🕐 {now_str}\n{'━'*22}\n", parse_mode="Markdown")
    for d, ana, entry in results:
        await bot.send_message(chat_id, build_card(d, ana, entry), parse_mode="Markdown")
        await asyncio.sleep(0.5)

    if alerts:
        lines = ["🚨 *ALERT PENTING:*\n"]
        for kode, atype, price, cl, tp in alerts:
            if atype == "CUT_LOSS":
                lines.append(f"⛔ *{kode}* → Rp {fmt(price)} ≤ CL Rp {fmt(cl)}\nSegera pertimbangkan *CUT LOSS!*")
            else:
                lines.append(f"🎯 *{kode}* → Rp {fmt(price)} ≥ TP Rp {fmt(tp)}\nPertimbangkan *TAKE PROFIT!*")
        await bot.send_message(chat_id, "\n\n".join(lines), parse_mode="Markdown")

async def scheduled_scan(app: Application):
    wl = load_watchlist()
    if not wl: return
    sess = "🌅 MARKET OPEN" if datetime.now(WIB).hour < 12 else "🌇 MARKET CLOSE"
    if ALLOWED_IDS:
        for uid in ALLOWED_IDS:
            try:
                await do_scan(app.bot, uid, wl, header=f"📊 *{sess}*")
            except Exception as e:
                logger.error(f"Scheduled scan error {uid}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    for cmd, handler in [
        ("start",      cmd_start),
        ("add",        cmd_add),
        ("del",        cmd_del),
        ("list",       cmd_list),
        ("cek",        cmd_cek),
        ("chart",      cmd_chart),
        ("setcl",      cmd_setcl),
        ("settp",      cmd_settp),
        ("scan",       cmd_scan),
        ("status",     cmd_status),
        ("testsource", cmd_testsource),
        ("help",       cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    scheduler = AsyncIOScheduler(timezone=WIB)
    scheduler.add_job(scheduled_scan, "cron", hour=9,  minute=0, args=[app])
    scheduler.add_job(scheduled_scan, "cron", hour=15, minute=0, args=[app])
    scheduler.start()

    logger.info("Bot started. IDX Official API only. Scheduler: 09:00 & 15:00 WIB.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
