"""
Bot Sinyal Saham IDX - Versi 3.0
Upgrade: Dynamic stock selection dari CSV GitHub (695 saham)
Fix: periode "1y" untuk cut loss & take profit
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import schedule
import time
import json
import os
from datetime import datetime, date
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# KONFIGURASI
# ============================================================
TELEGRAM_TOKEN = "GANTI_TOKEN_BARU_KAMU"
CHAT_ID        = "906923710"
PORTFOLIO_FILE = "portfolio.json"

# URL CSV di GitHub — ganti USERNAME dan REPO sesuai milikmu
CSV_URL = "https://raw.githubusercontent.com/Lasmatriani/SahamLT/main/saham_idx.csv"

# Cache lokal supaya tidak download ulang tiap scan
_cache_saham = None
_cache_time  = None
CACHE_HOURS  = 24  # refresh sekali sehari

# ============================================================
# FUNGSI TELEGRAM
# ============================================================
def kirim_pesan(pesan):
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": pesan, "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.error(f"Gagal kirim pesan: {r.text}")
    except Exception as e:
        logger.error(f"Error Telegram: {e}")

# ============================================================
# FUNGSI LOAD CSV SAHAM
# ============================================================
def load_saham_csv():
    """Download CSV dari GitHub, return dict {sektor: [ticker, ...]}"""
    global _cache_saham, _cache_time

    # Pakai cache kalau masih fresh
    if _cache_saham and _cache_time:
        selisih = (datetime.now() - _cache_time).total_seconds() / 3600
        if selisih < CACHE_HOURS:
            logger.info(f"Pakai cache saham ({len(_cache_saham)} sektor)")
            return _cache_saham

    logger.info("Download CSV saham dari GitHub...")
    try:
        r = requests.get(CSV_URL, timeout=15)
        r.raise_for_status()

        lines = r.text.strip().split("\n")
        sektor_dict = {}

        for line in lines[1:]:   # skip header
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            ticker = parts[0].strip()
            sektor = parts[1].strip()
            if sektor not in sektor_dict:
                sektor_dict[sektor] = []
            sektor_dict[sektor].append(ticker)

        total = sum(len(v) for v in sektor_dict.values())
        logger.info(f"CSV loaded: {total} saham, {len(sektor_dict)} sektor")

        _cache_saham = sektor_dict
        _cache_time  = datetime.now()
        return sektor_dict

    except Exception as e:
        logger.error(f"Gagal load CSV: {e}")
        kirim_pesan(f"⚠️ Gagal download CSV saham dari GitHub.\nError: {e}\n\nBot tetap berjalan tapi scan dilewati.")
        return {}

# ============================================================
# FUNGSI AMBIL DATA & INDIKATOR
# ============================================================
def ambil_data(ticker, periode="1y"):
    try:
        df = yf.Ticker(ticker).history(period=periode)
        if df.empty or len(df) < 50:
            return None
        return df
    except Exception as e:
        logger.error(f"Error ambil {ticker}: {e}")
        return None

def hitung_indikator(df):
    df = df.copy()
    df['MA50']     = df['Close'].rolling(50).mean()
    df['MA200']    = df['Close'].rolling(200).mean()
    df['MA20']     = df['Close'].rolling(20).mean()
    df['Vol_MA20'] = df['Volume'].rolling(20).mean()

    delta = df['Close'].diff()
    gain  = delta.where(delta > 0, 0).rolling(14).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['RSI'] = 100 - (100 / (1 + gain / loss))

    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD']      = ema12 - ema26
    df['Signal']    = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal']
    return df

# ============================================================
# SCORING SEKTOR HOT (pakai 5 proxy per sektor)
# ============================================================
def scoring_sektor(sektor_dict):
    """Score tiap sektor, return list [(sektor, skor)] sorted desc"""
    logger.info("Scoring sektor...")
    hasil = {}

    for sektor, tickers in sektor_dict.items():
        proxy   = tickers[:5]
        total   = 0
        valid   = 0

        for t in proxy:
            df = ambil_data(t, "3mo")
            if df is None or len(df) < 20:
                continue
            df  = hitung_indikator(df)
            lat = df.iloc[-1]
            prv = df.iloc[-2]
            valid += 1

            if lat['Close'] > prv['Close']:       total += 2   # naik hari ini
            if lat['Close'] > lat['MA50']:         total += 1   # di atas MA50
            if lat['Volume'] > lat['Vol_MA20']:    total += 1   # volume naik
            if lat['MACD']  > lat['Signal']:       total += 1   # MACD positif
            time.sleep(0.3)

        if valid:
            hasil[sektor] = round(total / valid, 2)

    return sorted(hasil.items(), key=lambda x: x[1], reverse=True)

def pilih_saham_dinamis(sektor_dict):
    """Pilih saham dari sektor hot, kirim ringkasan ke Telegram"""
    ranking = scoring_sektor(sektor_dict)
    if not ranking:
        # Fallback: ambil semua
        all_tickers = [t for v in sektor_dict.values() for t in v]
        return all_tickers

    # Bangun pesan kondisi sektor
    pesan = "🌡️ <b>Kondisi Sektor Hari Ini:</b>\n━━━━━━━━━━━━━━━━━━\n"
    for nama, skor in ranking:
        if skor >= 2.5:
            label = "🔥 HOT"
        elif skor >= 1.5:
            label = "🟡 WARM"
        else:
            label = "❄️ COLD"
        bar    = "█" * int(skor) + "░" * (5 - int(skor))
        jumlah = len(sektor_dict.get(nama, []))
        pesan += f"{label} <b>{nama}</b> ({jumlah} saham)\n    {bar} {skor:.1f}/5\n\n"
    kirim_pesan(pesan)

    sektor_hot  = [n for n, s in ranking if s >= 2.5]
    sektor_warm = [n for n, s in ranking if 1.5 <= s < 2.5]

    terpilih = []
    for s in sektor_hot:
        terpilih += sektor_dict.get(s, [])
    if len(sektor_hot) < 3:
        for s in sektor_warm[:2]:
            terpilih += sektor_dict.get(s, [])[:10]

    # Kalau semua cold, ambil top 3 sektor saja
    if not terpilih:
        kirim_pesan("❄️ <b>Semua sektor sedang cold.</b>\nBot tetap scan top 3 sektor terbaik.")
        for n, _ in ranking[:3]:
            terpilih += sektor_dict.get(n, [])[:8]

    # Hapus duplikat, pertahankan urutan
    seen = set()
    hasil = []
    for t in terpilih:
        if t not in seen:
            seen.add(t)
            hasil.append(t)

    logger.info(f"Saham terpilih: {len(hasil)} dari {len(sektor_hot)} sektor hot")
    return hasil

# ============================================================
# ANALISIS SINYAL BELI
# ============================================================
def analisis_beli(ticker):
    df = ambil_data(ticker, "1y")
    if df is None or len(df) < 50:
        return None

    df  = hitung_indikator(df)
    lat = df.iloc[-1]
    prv = df.iloc[-2]
    rsi = lat['RSI']

    skor   = 0
    detail = []

    if lat['MA50'] > lat['MA200']:
        skor += 1; detail.append("✅ Golden Cross aktif")
    else:
        detail.append("❌ Belum Golden Cross")

    if lat['Close'] > lat['MA50']:
        skor += 1; detail.append("✅ Harga > MA50")
    else:
        detail.append("❌ Harga < MA50")

    if 40 <= rsi <= 60:
        skor += 1; detail.append(f"✅ RSI {rsi:.1f} (ideal)")
    else:
        detail.append(f"⚠️ RSI {rsi:.1f} ({'overbought' if rsi > 60 else 'oversold'})")

    if lat['MACD'] > lat['Signal'] and prv['MACD'] <= prv['Signal']:
        skor += 1; detail.append("✅ MACD Bullish Cross baru!")
    elif lat['MACD'] > lat['Signal']:
        skor += 0.5; detail.append("✅ MACD di atas Signal")
    else:
        detail.append("❌ MACD Bearish")

    if lat['Volume'] > lat['Vol_MA20'] * 1.2:
        skor += 1
        pct = ((lat['Volume'] / lat['Vol_MA20']) - 1) * 100
        detail.append(f"✅ Volume +{pct:.0f}% di atas rata-rata")
    else:
        detail.append("❌ Volume lemah")

    return {
        'ticker': ticker, 'harga': lat['Close'], 'skor': skor,
        'rsi': rsi, 'ma50': lat['MA50'], 'ma200': lat['MA200'],
        'volume': lat['Volume'], 'vol_ma20': lat['Vol_MA20'], 'detail': detail
    }

# ============================================================
# ANALISIS CUT LOSS
# ============================================================
def analisis_cutloss(ticker, harga_beli):
    df = ambil_data(ticker, "1y")
    if df is None or len(df) < 50:
        return None

    df  = hitung_indikator(df)
    lat = df.iloc[-1]; prv = df.iloc[-2]; prv2 = df.iloc[-3]

    harga_skrg = lat['Close']
    pnl        = ((harga_skrg - harga_beli) / harga_beli) * 100
    pintu      = 0
    triggers   = []

    # Pintu 1: Hard Stop -7%
    if pnl <= -7:
        pintu += 1
        triggers.append(f"🚪 Pintu 1: Hard Stop ({pnl:.1f}%)")

    # Pintu 2: Breakdown teknikal
    death_cross    = lat['MA50'] < lat['MA200']
    bawah_ma50_2hr = lat['Close'] < lat['MA50'] and prv['Close'] < prv['MA50']
    macd_bearish   = lat['MACD'] < lat['Signal'] and prv['MACD'] >= prv['Signal']

    if death_cross or bawah_ma50_2hr or macd_bearish:
        pintu += 1
        if death_cross:    triggers.append("🚪 Pintu 2: Death Cross!")
        if bawah_ma50_2hr: triggers.append("🚪 Pintu 2: Harga < MA50 (2 hari)")
        if macd_bearish:   triggers.append("🚪 Pintu 2: MACD Bearish Cross")

    # Pintu 3: Momentum lemah
    rsi_lemah  = lat['RSI'] < 35
    vol_jual   = sum(1 for i in [-1,-2,-3] if df.iloc[i]['Close'] < df.iloc[i]['Open'])
    lower_low  = lat['Low'] < prv['Low'] < prv2['Low']

    if rsi_lemah or vol_jual >= 3 or lower_low:
        pintu += 1
        if rsi_lemah:   triggers.append(f"🚪 Pintu 3: RSI lemah ({lat['RSI']:.1f})")
        if vol_jual>=3: triggers.append("🚪 Pintu 3: Volume jual dominan 3 hari")
        if lower_low:   triggers.append("🚪 Pintu 3: Lower Low terkonfirmasi")

    return {
        'ticker': ticker, 'harga_beli': harga_beli,
        'harga_sekarang': harga_skrg, 'pnl_pct': pnl,
        'pintu_terbuka': pintu, 'triggers': triggers, 'rsi': lat['RSI']
    }

# ============================================================
# ANALISIS TAKE PROFIT
# ============================================================
def analisis_takeprofit(ticker, harga_beli):
    df = ambil_data(ticker, "1y")
    if df is None or len(df) < 50:
        return None

    df  = hitung_indikator(df)
    lat = df.iloc[-1]; prv = df.iloc[-2]

    harga_skrg = lat['Close']
    profit     = ((harga_skrg - harga_beli) / harga_beli) * 100
    if profit <= 0:
        return None

    skor_jual = 0; triggers = []; rek_jual = 0

    if profit >= 15:
        skor_jual += 1
        triggers.append(f"🎯 Target 1: Profit +{profit:.1f}%!")
        rek_jual = 50

    if lat['RSI'] > 75:
        skor_jual += 1
        triggers.append(f"🎯 Target 2: RSI {lat['RSI']:.1f} (overbought)")
        rek_jual = min(rek_jual + 30, 80)

    macd_lemah   = lat['MACD_Hist'] < prv['MACD_Hist'] and lat['MACD'] > lat['Signal']
    golden_lemah = (lat['MA50'] - lat['MA200']) < (prv['MA50'] - prv['MA200'])

    if macd_lemah or golden_lemah:
        skor_jual += 1
        triggers.append("🎯 Target 3: Momentum mulai melemah")
        rek_jual = min(rek_jual + 20, 100)

    if skor_jual == 0:
        return None

    return {
        'ticker': ticker, 'harga_beli': harga_beli,
        'harga_sekarang': harga_skrg, 'profit_pct': profit,
        'skor_jual': skor_jual, 'triggers': triggers,
        'rekomendasi_jual': rek_jual, 'rsi': lat['RSI']
    }

# ============================================================
# FORMAT PESAN
# ============================================================
def fmt_beli(d):
    pct = ((d['volume']/d['vol_ma20'])-1)*100
    bintang = "⭐"*int(d['skor'])
    return f"""🟢 <b>SINYAL BELI KUAT</b>
━━━━━━━━━━━━━━━━━━
📌 Saham  : <b>{d['ticker']}</b>
💰 Harga  : Rp {d['harga']:,.0f}
📊 Skor   : {d['skor']:.1f}/5 {bintang}
📈 RSI    : {d['rsi']:.1f}
📦 Volume : +{pct:.0f}% di atas rata-rata

<b>Detail:</b>
{chr(10).join(d['detail'])}

⚠️ <i>Bukan rekomendasi investasi. DYOR!</i>""".strip()

def fmt_cutloss(d):
    i   = min(d['pintu_terbuka']-1, 2)
    em  = ["⚠️","🔴","🆘"][i]
    lvl = ["WARNING","CUT LOSS KUAT","CUT LOSS DARURAT"][i]
    rek = ["Pantau ketat!","Pertimbangkan JUAL sekarang","JUAL SEGERA!"][i]
    return f"""{em} <b>{lvl}</b>
━━━━━━━━━━━━━━━━━━
📌 Saham         : <b>{d['ticker']}</b>
💰 Harga Beli    : Rp {d['harga_beli']:,.0f}
📉 Harga Skrg    : Rp {d['harga_sekarang']:,.0f}
📊 P&L           : {d['pnl_pct']:.1f}%
🚪 Pintu Terbuka : {d['pintu_terbuka']}/3
📊 RSI           : {d['rsi']:.1f}

<b>Trigger:</b>
{chr(10).join(d['triggers'])}

❗ <b>{rek}</b>""".strip()

def fmt_takeprofit(d):
    bintang = "💰"*d['skor_jual']
    return f"""💰 <b>SINYAL TAKE PROFIT</b>
━━━━━━━━━━━━━━━━━━
📌 Saham       : <b>{d['ticker']}</b>
💰 Harga Beli  : Rp {d['harga_beli']:,.0f}
📈 Harga Skrg  : Rp {d['harga_sekarang']:,.0f}
🎯 Profit      : +{d['profit_pct']:.1f}% {bintang}
📊 Skor Jual   : {d['skor_jual']}/3
📊 RSI         : {d['rsi']:.1f}

<b>Trigger:</b>
{chr(10).join(d['triggers'])}

✅ <b>Rekomendasi: Jual {d['rekomendasi_jual']}% posisi</b>""".strip()

# ============================================================
# PORTFOLIO
# ============================================================
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {}

def save_portfolio(p):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(p, f, indent=2)

# ============================================================
# SCAN UTAMA
# ============================================================
def scan_pagi():
    logger.info("Scan pagi mulai...")
    hari = datetime.now().strftime("%A, %d %B %Y")
    kirim_pesan(f"🌅 <b>Selamat Pagi!</b>\n📅 {hari}\n\n🔍 Memuat daftar saham IDX dari GitHub...")

    sektor_dict = load_saham_csv()
    if not sektor_dict:
        return

    total = sum(len(v) for v in sektor_dict.values())
    kirim_pesan(f"✅ <b>{total} saham</b> dari <b>{len(sektor_dict)} sektor</b> berhasil dimuat.\n\n🌡️ Scoring sektor, sabar sebentar...")

    terpilih = pilih_saham_dinamis(sektor_dict)
    kirim_pesan(f"📋 Scanning <b>{len(terpilih)} saham</b> dari sektor hot...\nEstimasi ~{len(terpilih)//2} menit ☕")

    sinyal = []
    for t in terpilih:
        try:
            h = analisis_beli(t)
            if h and h['skor'] >= 4:
                sinyal.append(h)
        except Exception as e:
            logger.error(f"Error scan {t}: {e}")
        time.sleep(0.5)

    sinyal.sort(key=lambda x: x['skor'], reverse=True)

    if sinyal:
        kirim_pesan(f"✅ <b>Ditemukan {len(sinyal)} sinyal beli kuat!</b>")
        for d in sinyal[:5]:
            kirim_pesan(fmt_beli(d))
            time.sleep(1)
    else:
        kirim_pesan("📊 <b>Hasil Scan Pagi</b>\n\nBelum ada sinyal beli kuat hari ini.\nMarket mungkin sideways atau bearish.\n\n💡 Tetap pantau portofoliomu ya!")

    scan_portofolio()

def scan_siang():
    logger.info("Scan siang...")
    kirim_pesan("📊 <b>Update Siang</b>\n🔍 Monitoring portofolio...")
    scan_portofolio()

def scan_sore():
    logger.info("Scan sore...")
    hari = datetime.now().strftime("%d %B %Y")
    kirim_pesan(f"🔔 <b>Closing Alert — {hari}</b>\n\nMonitoring posisi sebelum market tutup...")
    scan_portofolio()
    kirim_pesan("📋 <b>Recap</b>\nMarket IDX tutup pukul 16.00 WIB.\nPastikan posisimu sudah sesuai strategi! 💪\n\n⏰ Scan berikutnya besok 07.00 WIB")

def scan_portofolio():
    portfolio = load_portfolio()
    if not portfolio:
        kirim_pesan("💼 <b>Portofolio kosong</b>\n\nGunakan /tambah TICKER HARGA\nContoh: /tambah BBCA 6995")
        return

    ada_sinyal = False
    for ticker, data in portfolio.items():
        hb = data['harga_beli']

        tp = analisis_takeprofit(ticker, hb)
        if tp and tp['skor_jual'] >= 1:
            kirim_pesan(fmt_takeprofit(tp))
            ada_sinyal = True; time.sleep(1)

        cl = analisis_cutloss(ticker, hb)
        if cl and cl['pintu_terbuka'] >= 1:
            kirim_pesan(fmt_cutloss(cl))
            ada_sinyal = True; time.sleep(1)

    if not ada_sinyal:
        kirim_pesan("✅ <b>Portofolio Aman</b>\n\nSemua posisi dalam kondisi normal.\nTidak ada sinyal cut loss atau take profit saat ini. 👍")

# ============================================================
# COMMAND HANDLER
# ============================================================
last_update_id = 0

def cek_perintah():
    global last_update_id
    try:
        url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 5}
        r      = requests.get(url, params=params, timeout=10).json()

        if not r.get('ok') or not r.get('result'):
            return

        for upd in r['result']:
            last_update_id = upd['update_id']
            if 'message' not in upd:
                continue

            msg     = upd['message']
            text    = msg.get('text','').strip()
            chat_id = str(msg['chat']['id'])

            if chat_id != CHAT_ID:
                continue

            if text == '/start':
                kirim_pesan("""🤖 <b>Bot Sinyal Saham IDX v3.0</b>

✨ <b>Fitur:</b>
📂 695 saham IDX dari CSV GitHub
🔥 Dynamic scan sektor hot
✅ Fix cut loss & take profit

<b>Perintah:</b>
/portofolio — Lihat posisi + P&L
/tambah BBCA 6995 — Tambah saham
/hapus BBCA — Hapus saham
/scan — Scan manual sekarang
/sektor — Cek kondisi sektor
/reload — Reload CSV saham terbaru
/help — Bantuan

⏰ <b>Jadwal otomatis (Senin–Jumat):</b>
🌅 07.00 — Scan + scoring sektor
📊 12.00 — Monitor portofolio
🔔 15.45 — Closing alert""".strip())

            elif text == '/sektor':
                kirim_pesan("🌡️ Menganalisis sektor... sabar sebentar 😊")
                sektor_dict = load_saham_csv()
                if sektor_dict:
                    ranking = scoring_sektor(sektor_dict)
                    pesan   = "🌡️ <b>Kondisi Sektor:</b>\n━━━━━━━━━━━━━━━━━━\n"
                    for nm, sk in ranking:
                        lbl = "🔥 HOT" if sk>=2.5 else ("🟡 WARM" if sk>=1.5 else "❄️ COLD")
                        bar = "█"*int(sk) + "░"*(5-int(sk))
                        pesan += f"{lbl} <b>{nm}</b>\n    {bar} {sk:.1f}/5\n\n"
                    kirim_pesan(pesan)

            elif text == '/reload':
                global _cache_saham, _cache_time
                _cache_saham = None; _cache_time = None
                sektor_dict  = load_saham_csv()
                if sektor_dict:
                    total = sum(len(v) for v in sektor_dict.values())
                    kirim_pesan(f"✅ CSV berhasil di-reload!\n{total} saham dari {len(sektor_dict)} sektor.")

            elif text == '/portofolio':
                portfolio = load_portfolio()
                if not portfolio:
                    kirim_pesan("💼 Portofolio kosong.\n/tambah BBCA 6995")
                else:
                    pesan = "💼 <b>Portofolio:</b>\n━━━━━━━━━━━━━━━━━━\n"
                    for ticker, data in portfolio.items():
                        try:
                            hn  = yf.Ticker(ticker).history(period="2d")['Close'].iloc[-1]
                            pnl = ((hn - data['harga_beli']) / data['harga_beli']) * 100
                            em  = "📈" if pnl >= 0 else "📉"
                            pesan += f"{em} <b>{ticker}</b>\n"
                            pesan += f"   Beli : Rp {data['harga_beli']:,.0f}\n"
                            pesan += f"   Skrg : Rp {hn:,.0f}\n"
                            pesan += f"   P&L  : {pnl:+.1f}%\n\n"
                        except:
                            pesan += f"📌 <b>{ticker}</b> — Rp {data['harga_beli']:,.0f}\n\n"
                    kirim_pesan(pesan)

            elif text.startswith('/tambah'):
                parts = text.split()
                if len(parts) == 3:
                    t  = parts[1].upper()
                    if not t.endswith('.JK'): t += '.JK'
                    try:
                        hb = float(parts[2])
                        p  = load_portfolio()
                        p[t] = {'harga_beli': hb, 'tanggal': str(date.today())}
                        save_portfolio(p)
                        kirim_pesan(f"✅ <b>{t}</b> ditambahkan!\n💰 Harga beli: Rp {hb:,.0f}")
                    except:
                        kirim_pesan("❌ Format: /tambah BBCA 6995")
                else:
                    kirim_pesan("❌ Format: /tambah BBCA 6995")

            elif text.startswith('/hapus'):
                parts = text.split()
                if len(parts) == 2:
                    t = parts[1].upper()
                    if not t.endswith('.JK'): t += '.JK'
                    p = load_portfolio()
                    if t in p:
                        del p[t]; save_portfolio(p)
                        kirim_pesan(f"✅ <b>{t}</b> dihapus.")
                    else:
                        kirim_pesan(f"❌ {t} tidak ada di portofolio.")

            elif text == '/scan':
                kirim_pesan("🔍 Scan manual dimulai...")
                scan_pagi()

            elif text == '/help':
                kirim_pesan("""📖 <b>Panduan Bot v3.0</b>

/tambah BBCA 6995 — Tambah ke portofolio
/hapus BBCA — Hapus dari portofolio
/portofolio — Lihat posisi + P&L realtime
/sektor — Kondisi sektor hari ini
/scan — Scan manual
/reload — Refresh daftar saham dari CSV
/start — Info bot

<b>Sinyal otomatis:</b>
🔥 Scan sektor hot tiap pagi
🟢 Beli (skor 4–5/5)
💰 Take Profit bertingkat
⚠️ Warning / 🔴 Cut Loss / 🆘 Darurat

⚠️ <i>Hanya sinyal teknikal. DYOR!</i>""".strip())

    except Exception as e:
        logger.error(f"Error cek perintah: {e}")

# ============================================================
# SCHEDULER
# ============================================================
def setup_jadwal():
    for hari in ['monday','tuesday','wednesday','thursday','friday']:
        getattr(schedule.every(), hari).at("07:00").do(scan_pagi)
        getattr(schedule.every(), hari).at("12:00").do(scan_siang)
        getattr(schedule.every(), hari).at("15:45").do(scan_sore)
    logger.info("Jadwal OK!")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    logger.info("Bot IDX v3.0 starting...")
    kirim_pesan("""🚀 <b>Bot Sinyal Saham IDX v3.0 Aktif!</b>

✨ <b>Upgrade:</b>
📂 695 saham dari CSV GitHub (no hardcode!)
🔥 Dynamic scan sektor hot
✅ Fix analisis cut loss & take profit

⏰ <b>Jadwal (Senin–Jumat):</b>
🌅 07.00 — Scan + scoring sektor
📊 12.00 — Monitor portofolio
🔔 15.45 — Closing alert

Ketik /help untuk panduan lengkap 😊""".strip())

    setup_jadwal()
    logger.info("Bot berjalan!")

    while True:
        schedule.run_pending()
        cek_perintah()
        time.sleep(3)
