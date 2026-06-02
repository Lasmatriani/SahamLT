# 📊 IDX Saham Bot — Telegram

Bot Telegram untuk memantau saham IDX secara otomatis. Data dari Yahoo Finance, notifikasi Cut Loss & Take Profit otomatis, jadwal scan 2x sehari.

---

## ⚡ Fitur

| Fitur | Detail |
|---|---|
| 📥 Tambah/hapus saham | `/add` dan `/del` |
| 🔍 Cek saham realtime | `/cek KODE` |
| 📡 Auto scan | Setiap 09:00 & 15:00 WIB |
| 🚨 Alert CL/TP | Otomatis jika harga menyentuh level |
| 📊 Teknikal | RSI, MACD, MA20/50, Support/Resistance |
| 💹 Fundamental | PBV, PER dari Yahoo Finance |
| ⚙️ Custom CL/TP | Per saham, % + teknikal |

---

## 🚀 Deploy ke Railway

### 1. Buat Bot Telegram
1. Chat [@BotFather](https://t.me/botfather) di Telegram
2. Ketik `/newbot` → ikuti instruksi
3. Copy **BOT_TOKEN** yang diberikan

### 2. Dapatkan User ID kamu
1. Chat [@userinfobot](https://t.me/userinfobot)
2. Catat angka **Id** yang muncul

### 3. Upload ke GitHub
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/USERNAME/saham-bot.git
git push -u origin main
```

### 4. Deploy di Railway
1. Buka [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
2. Pilih repo saham-bot
3. Masuk ke tab **Variables**, tambahkan:

| Key | Value |
|---|---|
| `BOT_TOKEN` | token dari BotFather |
| `ALLOWED_USER_IDS` | user ID Telegram kamu |
| `DEFAULT_CL_PCT` | `-7` (opsional) |
| `DEFAULT_TP_PCT` | `15` (opsional) |

4. Railway otomatis deploy. Selesai! ✅

---

## 📱 Cara Pakai Bot

### Tambah saham
```
/add ANTM 2900
/add BBRI 4500
/add GGRM 16950
```

### Hapus saham
```
/del ANTM
```

### Lihat watchlist
```
/list
```

### Cek saham sekarang
```
/cek ANTM
/cek BBRI
```

### Custom Cut Loss & Take Profit
```
/setcl ANTM -8        → CL di -8% dari entry
/settp ANTM 20        → TP di +20% dari entry
```

### Scan semua watchlist manual
```
/scan
```

---

## 🧠 Logika CL & TP

Bot menggunakan **2 metode**, diambil yang lebih konservatif:

**Teknikal:**
- CL teknikal = 1% di bawah Support 20-hari
- TP teknikal = Resistance 20-hari

**Persentase (backup):**
- CL % = entry × (1 + CL%)  → default -7%
- TP % = entry × (1 + TP%)  → default +15%

**Final:**
- CL final = yang **lebih tinggi** (lebih protektif)
- TP final = yang **lebih rendah** (lebih konservatif)

---

## 📊 Indikator Teknikal

| Indikator | Detail |
|---|---|
| RSI 14 | < 30 oversold (buy), > 70 overbought (sell) |
| MACD 12/26/9 | Histogram positif = bullish |
| MA20 & MA50 | Harga di atas = bullish |
| Volume | Ratio vs rata-rata 10 hari |
| Support/Resistance | Low/High 20-hari rolling |

---

## ⚠️ Disclaimer
Bot ini hanya untuk edukasi dan membantu monitoring. Bukan rekomendasi beli/jual. DYOR.
