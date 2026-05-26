# Bot Sinyal Saham IDX 📈

Bot Telegram otomatis untuk sinyal beli, cut loss, dan take profit saham IDX.

## Fitur
- 🟢 Sinyal Beli (Multi-Factor Scoring)
- 🔴 Cut Loss 3 Pintu (Warning → Kuat → Darurat)
- 💰 Take Profit Bertingkat (50% → 80% → 100%)
- 📱 Notifikasi Telegram otomatis 3x sehari
- 💼 Portfolio tracker via perintah Telegram

## Deploy ke Railway.app (GRATIS)

### Step 1: Buat akun Railway
1. Buka https://railway.app
2. Daftar dengan GitHub (gratis)

### Step 2: Upload ke GitHub
1. Buat repo baru di GitHub
2. Upload semua file ini (bot.py, requirements.txt, Procfile, runtime.txt)

### Step 3: Deploy di Railway
1. Klik "New Project" di Railway
2. Pilih "Deploy from GitHub repo"
3. Pilih repo kamu
4. Railway otomatis detect Python dan deploy!

### Step 4: Cek logs
- Lihat logs di Railway untuk pastikan bot berjalan
- Cek Telegram, harusnya ada pesan startup

## Perintah Telegram
- `/start` - Mulai bot
- `/tambah BBCA 9000` - Tambah saham ke portofolio
- `/hapus BBCA` - Hapus dari portofolio
- `/portofolio` - Lihat semua posisi + P&L
- `/scan` - Scan manual
- `/help` - Bantuan lengkap

## Jadwal Otomatis
- 🌅 07.00 WIB - Scan sinyal beli (Senin-Jumat)
- 📊 12.00 WIB - Monitor portofolio
- 🔔 15.45 WIB - Closing alert

## ⚠️ Disclaimer
Bot ini hanya pemberi sinyal analisis teknikal.
Bukan rekomendasi investasi. Selalu lakukan riset sendiri (DYOR).
