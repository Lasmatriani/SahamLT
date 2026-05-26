"""
Bot Sinyal Saham IDX
Strategi: Multi-Factor (Beli, Cut Loss, Take Profit)
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
# KONFIGURASI - Ganti dengan data kamu
# ============================================================
TELEGRAM_TOKEN = "8862850675:AAF3qBDI4YCwRuTqSXLZfy33IKzsdzYwyG4"
CHAT_ID = "906923710"

# Saham IDX yang dipantau (top liquid stocks)
SAHAM_IDX = [
    "BBCA.JK", "BBRI.JK", "BMRI.JK", "BBNI.JK", "BRIS.JK",
    "TLKM.JK", "ASII.JK", "UNVR.JK", "ICBP.JK", "INDF.JK",
    "KLBF.JK", "SIDO.JK", "GGRM.JK", "HMSP.JK", "MYOR.JK",
    "ANTM.JK", "PTBA.JK", "ADRO.JK", "INCO.JK", "MEDC.JK",
    "SMGR.JK", "INTP.JK", "WIKA.JK", "WSKT.JK", "PTPP.JK",
    "EXCL.JK", "ISAT.JK", "TOWR.JK", "MTEL.JK", "TBIG.JK",
    "AALI.JK", "LSIP.JK", "SIMP.JK", "SSMS.JK", "PALM.JK",
    "JPFA.JK", "CPIN.JK", "MAIN.JK", "ULTJ.JK", "ROTI.JK",
    "PGAS.JK", "AKRA.JK", "AMRT.JK", "ACES.JK", "MAPI.JK",
    "BUKA.JK", "GOTO.JK", "EMTK.JK", "FILM.JK", "MDKA.JK"
]

# File untuk tracking portofolio
PORTFOLIO_FILE = "portfolio.json"

# ============================================================
# FUNGSI TELEGRAM
# ============================================================
def kirim_pesan(pesan):
    """Kirim pesan ke Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": pesan,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Pesan terkirim ke Telegram")
        else:
            logger.error(f"Gagal kirim pesan: {response.text}")
    except Exception as e:
        logger.error(f"Error kirim Telegram: {e}")

# ============================================================
# FUNGSI AMBIL DATA
# ============================================================
def ambil_data(ticker, periode="6mo"):
    """Ambil data historis saham dari Yahoo Finance"""
    try:
        saham = yf.Ticker(ticker)
        df = saham.history(period=periode)
        if df.empty or len(df) < 50:
            return None
        return df
    except Exception as e:
        logger.error(f"Error ambil data {ticker}: {e}")
        return None

def hitung_indikator(df):
    """Hitung semua indikator teknikal"""
    df = df.copy()
    
    # Moving Average
    df['MA50'] = df['Close'].rolling(window=50).mean()
    df['MA200'] = df['Close'].rolling(window=200).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    
    # RSI
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal']
    
    # Volume rata-rata
    df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()
    
    return df

# ============================================================
# FUNGSI ANALISIS SINYAL BELI
# ============================================================
def analisis_beli(ticker):
    """Analisis sinyal beli dengan scoring 0-5"""
    df = ambil_data(ticker)
    if df is None or len(df) < 200:
        return None
    
    df = hitung_indikator(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    skor = 0
    detail = []
    
    # 1. Golden Cross (MA50 > MA200)
    if latest['MA50'] > latest['MA200']:
        skor += 1
        detail.append("✅ Golden Cross aktif")
    else:
        detail.append("❌ Belum Golden Cross")
    
    # 2. Harga di atas MA50
    if latest['Close'] > latest['MA50']:
        skor += 1
        detail.append("✅ Harga > MA50")
    else:
        detail.append("❌ Harga < MA50")
    
    # 3. RSI antara 40-60
    rsi = latest['RSI']
    if 40 <= rsi <= 60:
        skor += 1
        detail.append(f"✅ RSI: {rsi:.1f} (ideal)")
    else:
        detail.append(f"⚠️ RSI: {rsi:.1f} ({'overbought' if rsi > 60 else 'oversold'})")
    
    # 4. MACD Bullish Cross
    if latest['MACD'] > latest['Signal'] and prev['MACD'] <= prev['Signal']:
        skor += 1
        detail.append("✅ MACD Bullish Cross baru!")
    elif latest['MACD'] > latest['Signal']:
        skor += 0.5
        detail.append("✅ MACD di atas Signal")
    else:
        detail.append("❌ MACD Bearish")
    
    # 5. Volume di atas rata-rata
    if latest['Volume'] > latest['Vol_MA20'] * 1.2:
        skor += 1
        vol_pct = ((latest['Volume'] / latest['Vol_MA20']) - 1) * 100
        detail.append(f"✅ Volume +{vol_pct:.0f}% di atas rata-rata")
    else:
        detail.append("❌ Volume lemah")
    
    return {
        'ticker': ticker,
        'harga': latest['Close'],
        'skor': skor,
        'rsi': rsi,
        'ma50': latest['MA50'],
        'ma200': latest['MA200'],
        'volume': latest['Volume'],
        'vol_ma20': latest['Vol_MA20'],
        'detail': detail
    }

# ============================================================
# FUNGSI ANALISIS CUT LOSS
# ============================================================
def analisis_cutloss(ticker, harga_beli):
    """Analisis 3 pintu cut loss"""
    df = ambil_data(ticker, periode="3mo")
    if df is None or len(df) < 50:
        return None
    
    df = hitung_indikator(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    harga_sekarang = latest['Close']
    pnl_pct = ((harga_sekarang - harga_beli) / harga_beli) * 100
    
    pintu_terbuka = 0
    triggers = []
    
    # PINTU 1: Hard Stop -7%
    pintu1 = pnl_pct <= -7
    if pintu1:
        pintu_terbuka += 1
        triggers.append(f"🚪 Pintu 1: Hard Stop ({pnl_pct:.1f}%)")
    
    # PINTU 2: Breakdown Teknikal
    death_cross = latest['MA50'] < latest['MA200']
    bawah_ma50_2hari = (latest['Close'] < latest['MA50']) and (prev['Close'] < prev['MA50'])
    macd_bearish = latest['MACD'] < latest['Signal'] and prev['MACD'] >= prev['Signal']
    
    pintu2 = death_cross or bawah_ma50_2hari or macd_bearish
    if pintu2:
        pintu_terbuka += 1
        if death_cross:
            triggers.append("🚪 Pintu 2: Death Cross terjadi!")
        if bawah_ma50_2hari:
            triggers.append("🚪 Pintu 2: Harga < MA50 (2 hari)")
        if macd_bearish:
            triggers.append("🚪 Pintu 2: MACD Bearish Cross")
    
    # PINTU 3: Momentum Lemah
    rsi_lemah = latest['RSI'] < 35
    
    # Cek volume jual dominan 3 hari
    vol_jual_dominan = 0
    for i in [-1, -2, -3]:
        row = df.iloc[i]
        if row['Close'] < row['Open']:  # candle merah = jual dominan
            vol_jual_dominan += 1
    
    # Lower Low & Lower High
    lower_low = latest['Low'] < prev['Low'] < prev2['Low']
    
    pintu3 = rsi_lemah or (vol_jual_dominan >= 3) or lower_low
    if pintu3:
        pintu_terbuka += 1
        if rsi_lemah:
            triggers.append(f"🚪 Pintu 3: RSI lemah ({latest['RSI']:.1f})")
        if vol_jual_dominan >= 3:
            triggers.append("🚪 Pintu 3: Volume jual dominan 3 hari")
        if lower_low:
            triggers.append("🚪 Pintu 3: Tren turun (Lower Low)")
    
    return {
        'ticker': ticker,
        'harga_beli': harga_beli,
        'harga_sekarang': harga_sekarang,
        'pnl_pct': pnl_pct,
        'pintu_terbuka': pintu_terbuka,
        'triggers': triggers,
        'rsi': latest['RSI']
    }

# ============================================================
# FUNGSI ANALISIS TAKE PROFIT
# ============================================================
def analisis_takeprofit(ticker, harga_beli):
    """Analisis sinyal take profit bertingkat"""
    df = ambil_data(ticker, periode="3mo")
    if df is None or len(df) < 50:
        return None
    
    df = hitung_indikator(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    
    harga_sekarang = latest['Close']
    profit_pct = ((harga_sekarang - harga_beli) / harga_beli) * 100
    
    if profit_pct <= 0:
        return None  # Belum profit, skip
    
    skor_jual = 0
    triggers = []
    rekomendasi_jual = 0
    
    # Target 1: Profit +15%
    if profit_pct >= 15:
        skor_jual += 1
        triggers.append(f"🎯 Target 1: Profit +{profit_pct:.1f}% (target 15% tercapai!)")
        rekomendasi_jual = 50
    
    # Target 2: RSI Overbought > 75
    if latest['RSI'] > 75:
        skor_jual += 1
        triggers.append(f"🎯 Target 2: RSI {latest['RSI']:.1f} (overbought)")
        rekomendasi_jual = min(rekomendasi_jual + 30, 80)
    
    # Target 3: MACD mulai melemah
    macd_melemah = (latest['MACD_Hist'] < prev['MACD_Hist']) and (latest['MACD'] > latest['Signal'])
    golden_melemah = (latest['MA50'] - latest['MA200']) < (prev['MA50'] - prev['MA200'])
    
    if macd_melemah or golden_melemah:
        skor_jual += 1
        triggers.append("🎯 Target 3: Momentum mulai melemah")
        rekomendasi_jual = min(rekomendasi_jual + 20, 100)
    
    if skor_jual == 0:
        return None  # Belum ada sinyal TP
    
    return {
        'ticker': ticker,
        'harga_beli': harga_beli,
        'harga_sekarang': harga_sekarang,
        'profit_pct': profit_pct,
        'skor_jual': skor_jual,
        'triggers': triggers,
        'rekomendasi_jual': rekomendasi_jual,
        'rsi': latest['RSI']
    }

# ============================================================
# FUNGSI FORMAT PESAN
# ============================================================
def format_pesan_beli(data):
    vol_pct = ((data['volume'] / data['vol_ma20']) - 1) * 100
    bintang = "⭐" * int(data['skor'])
    
    pesan = f"""
🟢 <b>SINYAL BELI KUAT</b>
━━━━━━━━━━━━━━━━━━
📌 Saham  : <b>{data['ticker']}</b>
💰 Harga  : Rp {data['harga']:,.0f}
📊 Skor   : {data['skor']:.1f}/5 {bintang}
📈 RSI    : {data['rsi']:.1f}
📦 Volume : +{vol_pct:.0f}% di atas rata-rata

<b>Detail Indikator:</b>
{chr(10).join(data['detail'])}

⚠️ <i>Bukan rekomendasi investasi. DYOR!</i>
"""
    return pesan.strip()

def format_pesan_cutloss(data):
    emoji_level = ["⚠️", "🔴", "🆘"][min(data['pintu_terbuka']-1, 2)]
    level_text = ["WARNING", "CUT LOSS KUAT", "CUT LOSS DARURAT"][min(data['pintu_terbuka']-1, 2)]
    rekomendasi = ["Pantau ketat!", "Pertimbangkan JUAL sekarang", "JUAL SEGERA!"][min(data['pintu_terbuka']-1, 2)]
    
    pesan = f"""
{emoji_level} <b>{level_text}</b>
━━━━━━━━━━━━━━━━━━
📌 Saham         : <b>{data['ticker']}</b>
💰 Harga Beli    : Rp {data['harga_beli']:,.0f}
📉 Harga Skrg    : Rp {data['harga_sekarang']:,.0f}
📊 P&L           : {data['pnl_pct']:.1f}%
🚪 Pintu Terbuka : {data['pintu_terbuka']}/3
📊 RSI           : {data['rsi']:.1f}

<b>Trigger:</b>
{chr(10).join(data['triggers'])}

❗ <b>{rekomendasi}</b>
"""
    return pesan.strip()

def format_pesan_takeprofit(data):
    bintang = "💰" * data['skor_jual']
    
    pesan = f"""
💰 <b>SINYAL TAKE PROFIT</b>
━━━━━━━━━━━━━━━━━━
📌 Saham       : <b>{data['ticker']}</b>
💰 Harga Beli  : Rp {data['harga_beli']:,.0f}
📈 Harga Skrg  : Rp {data['harga_sekarang']:,.0f}
🎯 Profit      : +{data['profit_pct']:.1f}% {bintang}
📊 Skor Jual   : {data['skor_jual']}/3
📊 RSI         : {data['rsi']:.1f}

<b>Trigger:</b>
{chr(10).join(data['triggers'])}

✅ <b>Rekomendasi: Jual {data['rekomendasi_jual']}% posisi</b>
"""
    return pesan.strip()

# ============================================================
# FUNGSI PORTFOLIO TRACKER
# ============================================================
def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(portfolio, f, indent=2)

# ============================================================
# FUNGSI SCAN UTAMA
# ============================================================
def scan_pagi():
    """Scan pagi - cari sinyal beli + monitor portofolio"""
    logger.info("Mulai scan pagi...")
    hari = datetime.now().strftime("%A, %d %B %Y")
    
    kirim_pesan(f"🌅 <b>Selamat Pagi!</b>\n📅 {hari}\n\n🔍 Memulai scan saham IDX...\nSabar ya, ini butuh beberapa menit 😊")
    
    sinyal_beli = []
    
    for ticker in SAHAM_IDX:
        try:
            hasil = analisis_beli(ticker)
            if hasil and hasil['skor'] >= 4:
                sinyal_beli.append(hasil)
                logger.info(f"Sinyal beli: {ticker} skor {hasil['skor']}")
        except Exception as e:
            logger.error(f"Error scan {ticker}: {e}")
        time.sleep(0.5)  # Hindari rate limit
    
    # Sort by skor tertinggi
    sinyal_beli.sort(key=lambda x: x['skor'], reverse=True)
    
    if sinyal_beli:
        kirim_pesan(f"✅ <b>Scan selesai!</b>\nDitemukan <b>{len(sinyal_beli)} saham</b> dengan sinyal beli kuat!\n")
        for data in sinyal_beli[:5]:  # Max 5 sinyal teratas
            kirim_pesan(format_pesan_beli(data))
            time.sleep(1)
    else:
        kirim_pesan("📊 <b>Hasil Scan Pagi</b>\n\nBelum ada sinyal beli kuat hari ini.\nMarket mungkin sideways atau bearish.\n\n💡 Tetap pantau portofoliomu ya!")
    
    # Monitor portofolio untuk cut loss & TP
    scan_portofolio()

def scan_siang():
    """Scan siang - monitor cut loss & take profit"""
    logger.info("Mulai scan siang...")
    kirim_pesan("📊 <b>Update Siang</b>\n🔍 Monitoring portofolio...\n")
    scan_portofolio()

def scan_sore():
    """Scan sore - closing alert + recap"""
    logger.info("Mulai scan sore...")
    
    hari = datetime.now().strftime("%d %B %Y")
    kirim_pesan(f"🔔 <b>Closing Alert - {hari}</b>\n\n📊 Monitoring posisi sebelum market tutup...\n")
    
    scan_portofolio()
    
    # Recap singkat
    kirim_pesan("📋 <b>Recap Hari Ini</b>\n\nMarket IDX akan tutup pukul 16.00 WIB.\nPastikan posisimu sudah sesuai strategi! 💪\n\n⏰ Scan berikutnya besok pagi 07.00 WIB")

def scan_portofolio():
    """Scan portofolio untuk cut loss & take profit"""
    portfolio = load_portfolio()
    
    if not portfolio:
        kirim_pesan("💼 <b>Portofolio kosong</b>\n\nBelum ada saham yang dipantau.\nKirim perintah /tambah untuk tambah saham ke portofolio.")
        return
    
    ada_sinyal = False
    
    for ticker, data in portfolio.items():
        harga_beli = data['harga_beli']
        
        # Cek take profit dulu
        tp = analisis_takeprofit(ticker, harga_beli)
        if tp and tp['skor_jual'] >= 1:
            kirim_pesan(format_pesan_takeprofit(tp))
            ada_sinyal = True
            time.sleep(1)
        
        # Cek cut loss
        cl = analisis_cutloss(ticker, harga_beli)
        if cl and cl['pintu_terbuka'] >= 1:
            kirim_pesan(format_pesan_cutloss(cl))
            ada_sinyal = True
            time.sleep(1)
    
    if not ada_sinyal:
        kirim_pesan("✅ <b>Portofolio Aman</b>\n\nSemua posisi dalam kondisi normal.\nTidak ada sinyal cut loss atau take profit saat ini. 👍")

# ============================================================
# COMMAND HANDLER (via polling sederhana)
# ============================================================
last_update_id = 0

def cek_perintah():
    """Cek perintah dari user via Telegram"""
    global last_update_id
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 5}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        if not data.get('ok') or not data.get('result'):
            return
        
        for update in data['result']:
            last_update_id = update['update_id']
            
            if 'message' not in update:
                continue
                
            msg = update['message']
            text = msg.get('text', '').strip()
            chat_id = str(msg['chat']['id'])
            
            if chat_id != CHAT_ID:
                continue
            
            if text == '/start':
                kirim_pesan("""
🤖 <b>Bot Sinyal Saham IDX Aktif!</b>

<b>Perintah yang tersedia:</b>
/portofolio - Lihat portofolio
/tambah BBCA 9000 - Tambah saham (ticker & harga beli)
/hapus BBCA - Hapus dari portofolio
/scan - Scan manual sekarang
/help - Bantuan

⏰ <b>Jadwal otomatis:</b>
🌅 07.00 - Scan sinyal beli
📊 12.00 - Monitor portofolio
🔔 15.45 - Closing alert
                """.strip())
            
            elif text == '/portofolio':
                portfolio = load_portfolio()
                if not portfolio:
                    kirim_pesan("💼 Portofolio kosong.\n\nGunakan /tambah TICKER HARGA\nContoh: /tambah BBCA 9000")
                else:
                    pesan = "💼 <b>Portofolio Kamu:</b>\n━━━━━━━━━━━━━━━━━━\n"
                    for ticker, data in portfolio.items():
                        try:
                            saham = yf.Ticker(ticker)
                            harga_now = saham.history(period="1d")['Close'].iloc[-1]
                            pnl = ((harga_now - data['harga_beli']) / data['harga_beli']) * 100
                            emoji = "📈" if pnl >= 0 else "📉"
                            pesan += f"{emoji} <b>{ticker}</b>\n"
                            pesan += f"   Beli: Rp {data['harga_beli']:,.0f}\n"
                            pesan += f"   Skrg: Rp {harga_now:,.0f}\n"
                            pesan += f"   P&L: {pnl:+.1f}%\n\n"
                        except:
                            pesan += f"📌 <b>{ticker}</b> - Beli: Rp {data['harga_beli']:,.0f}\n\n"
                    kirim_pesan(pesan)
            
            elif text.startswith('/tambah'):
                parts = text.split()
                if len(parts) == 3:
                    ticker = parts[1].upper()
                    if not ticker.endswith('.JK'):
                        ticker += '.JK'
                    try:
                        harga_beli = float(parts[2])
                        portfolio = load_portfolio()
                        portfolio[ticker] = {'harga_beli': harga_beli, 'tanggal': str(date.today())}
                        save_portfolio(portfolio)
                        kirim_pesan(f"✅ <b>{ticker}</b> ditambahkan!\n💰 Harga beli: Rp {harga_beli:,.0f}\n\nBot akan monitor cut loss & take profit otomatis.")
                    except:
                        kirim_pesan("❌ Format salah!\nContoh: /tambah BBCA 9000")
                else:
                    kirim_pesan("❌ Format: /tambah TICKER HARGA\nContoh: /tambah BBCA 9000")
            
            elif text.startswith('/hapus'):
                parts = text.split()
                if len(parts) == 2:
                    ticker = parts[1].upper()
                    if not ticker.endswith('.JK'):
                        ticker += '.JK'
                    portfolio = load_portfolio()
                    if ticker in portfolio:
                        del portfolio[ticker]
                        save_portfolio(portfolio)
                        kirim_pesan(f"✅ <b>{ticker}</b> dihapus dari portofolio.")
                    else:
                        kirim_pesan(f"❌ {ticker} tidak ada di portofolio.")
            
            elif text == '/scan':
                kirim_pesan("🔍 Memulai scan manual...")
                scan_pagi()
            
            elif text == '/help':
                kirim_pesan("""
📖 <b>Panduan Bot Sinyal Saham IDX</b>

<b>Perintah:</b>
/tambah BBCA 9000 → Tambah BBCA dengan harga beli 9000
/hapus BBCA → Hapus BBCA dari portofolio
/portofolio → Lihat semua posisi + P&L
/scan → Scan manual sinyal beli
/start → Mulai ulang bot

<b>Sinyal yang dikirim otomatis:</b>
🟢 Sinyal Beli (skor 4-5/5)
💰 Take Profit (profit 15%+)
⚠️ Warning Cut Loss (1 pintu)
🔴 Cut Loss Kuat (2 pintu)
🆘 Cut Loss Darurat (3 pintu)

⚠️ <i>Bot ini hanya pemberi sinyal.
Keputusan beli/jual tetap di tanganmu!</i>
                """.strip())
    
    except Exception as e:
        logger.error(f"Error cek perintah: {e}")

# ============================================================
# SCHEDULER
# ============================================================
def setup_jadwal():
    """Setup jadwal otomatis"""
    schedule.every().monday.at("07:00").do(scan_pagi)
    schedule.every().tuesday.at("07:00").do(scan_pagi)
    schedule.every().wednesday.at("07:00").do(scan_pagi)
    schedule.every().thursday.at("07:00").do(scan_pagi)
    schedule.every().friday.at("07:00").do(scan_pagi)
    
    schedule.every().monday.at("12:00").do(scan_siang)
    schedule.every().tuesday.at("12:00").do(scan_siang)
    schedule.every().wednesday.at("12:00").do(scan_siang)
    schedule.every().thursday.at("12:00").do(scan_siang)
    schedule.every().friday.at("12:00").do(scan_siang)
    
    schedule.every().monday.at("15:45").do(scan_sore)
    schedule.every().tuesday.at("15:45").do(scan_sore)
    schedule.every().wednesday.at("15:45").do(scan_sore)
    schedule.every().thursday.at("15:45").do(scan_sore)
    schedule.every().friday.at("15:45").do(scan_sore)
    
    logger.info("Jadwal berhasil disetup!")

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    logger.info("Bot Sinyal Saham IDX starting...")
    
    # Kirim pesan startup
    kirim_pesan("""
🚀 <b>Bot Sinyal Saham IDX Aktif!</b>

✅ Semua sistem berjalan normal
📊 Monitoring 50+ saham IDX

⏰ <b>Jadwal scan otomatis:</b>
🌅 07.00 - Sinyal beli
📊 12.00 - Monitor portofolio  
🔔 15.45 - Closing alert

Ketik /help untuk panduan lengkap
    """.strip())
    
    setup_jadwal()
    
    logger.info("Bot berjalan! Menunggu jadwal...")
    
    while True:
        schedule.run_pending()
        cek_perintah()
        time.sleep(3)
