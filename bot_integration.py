"""
bot_integration.py
==================
Potongan kode untuk diintegrasikan ke bot.py yang sudah ada.
Salin bagian-bagian di bawah ke handler yang sesuai.
"""

# ════════════════════════════════════════════════════════════
# 1. IMPORT — taruh di atas bot.py
# ════════════════════════════════════════════════════════════
#
#   from data_source import get_current_price, get_stock_data, test_sources
#


# ════════════════════════════════════════════════════════════
# 2. /cek  — harga terkini
# ════════════════════════════════════════════════════════════

async def cmd_cek(update, context):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Gunakan: /cek KODE\nContoh: /cek BBRI"
        )
        return

    code = context.args[0].upper()
    msg  = await update.message.reply_text(f"⏳ Mengambil harga {code}...")

    price = get_current_price(code)

    if not price or price["close"] <= 0:
        await msg.edit_text(
            f"❌ Data untuk *{code}* tidak ditemukan.\n"
            "Pastikan kode saham benar (contoh: BBRI, TLKM, GOTO).",
            parse_mode="Markdown"
        )
        return

    arah  = "🟢" if price["change"] >= 0 else "🔴"
    tanda = "+" if price["change"] >= 0 else ""
    nama  = f" - _{price['name']}_" if price.get("name") else ""

    text = (
        f"📊 *{price['code']}*{nama}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Harga  : Rp {price['close']:,.0f}\n"
        f"{arah} Change : {tanda}{price['change']:,.0f} ({tanda}{price['change_pct']:.2f}%)\n"
        f"📈 High   : Rp {price['high']:,.0f}\n"
        f"📉 Low    : Rp {price['low']:,.0f}\n"
        f"🔓 Open   : Rp {price['open']:,.0f}\n"
        f"📦 Volume : {price['volume']:,}\n"
        f"📅 Tanggal: {price['date']}\n"
        f"🔌 Sumber : {price['source']}"
    )
    await msg.edit_text(text, parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
# 3. /chart  — ganti bagian fetch data lama
# ════════════════════════════════════════════════════════════

async def cmd_chart(update, context):
    if not context.args:
        await update.message.reply_text("⚠️ Gunakan: /chart KODE\nContoh: /chart TLKM")
        return

    code = context.args[0].upper()
    msg  = await update.message.reply_text(f"⏳ Membuat chart {code}...")

    # 200 hari = cukup untuk MA50, RSI(14), MACD(26), BB(20)
    df = get_stock_data(code, days=200)

    if df is None or len(df) < 20:
        await msg.edit_text(
            f"❌ Data historis *{code}* tidak cukup untuk chart.\n"
            "Minimal 20 hari data diperlukan.",
            parse_mode="Markdown"
        )
        return

    try:
        buf = _generate_chart(code, df)   # fungsi chart existing di bot.py
        await msg.delete()
        await update.message.reply_photo(
            photo=buf,
            caption=f"📊 *{code}* — {len(df)} hari data",
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Gagal membuat chart: {e}")


# ════════════════════════════════════════════════════════════
# 4. /testsource  — diagnostik
# ════════════════════════════════════════════════════════════

async def cmd_testsource(update, context):
    code = context.args[0].upper() if context.args else "BBRI"
    msg  = await update.message.reply_text(f"🔧 Menguji semua source untuk *{code}*...", parse_mode="Markdown")

    results = test_sources(code)

    lines = [f"🔌 *Test Source: {code}*", "━━━━━━━━━━━━━━━"]
    labels = {
        "idx_summary": "IDX Summary (real-time)",
        "idx_chart":   "IDX Chart (historis)",
        "yfinance":    "yfinance (.JK)",
        "stooq":       "Stooq (.ID)",
    }
    for key, label in labels.items():
        status = results.get(key, "?")
        lines.append(f"• *{label}*: `{status}`")

    lines.append("\n_IDX Summary → /cek_")
    lines.append("_IDX Chart / yfinance / Stooq → /chart, /scan_")

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


# ════════════════════════════════════════════════════════════
# 5. /scan  — ganti bagian fetch data lama
# ════════════════════════════════════════════════════════════

async def cmd_scan(update, context):
    chat_id   = update.effective_chat.id
    watchlist = _load_watchlist(chat_id)   # fungsi existing

    if not watchlist:
        await update.message.reply_text(
            "📋 Watchlist kosong.\nTambahkan saham dulu: /add KODE"
        )
        return

    msg = await update.message.reply_text(
        f"🔍 Scanning {len(watchlist)} saham..."
    )

    hasil = []
    for code in watchlist:
        df = get_stock_data(code, days=60)
        if df is None or len(df) < 20:
            hasil.append(f"⚠️ *{code}*: data tidak tersedia")
            continue

        sinyal = _analyze_signals(code, df)   # fungsi existing
        hasil.append(sinyal)

    text = "📊 *Hasil Scan Watchlist*\n━━━━━━━━━━━━━━━\n" + "\n".join(hasil)
    await msg.edit_text(text, parse_mode="Markdown")
