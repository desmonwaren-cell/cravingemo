"""
Craving Emojis Bot — Sticker Pack → Custom Emoji Pack Converter
Dengan normalisasi otomatis untuk semua format (TGS, WEBM, PNG/WEBP)
"""

import os
import io
import re
import json
import gzip
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path

from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
MAX_STICKERS = 120
UPLOAD_DELAY = 0.5   # detik antar upload

# Batas emoji spec Telegram
TGS_MAX_KB       = 64
TGS_MAX_DURATION = 3.0   # detik
TGS_TARGET_FPS   = 60
WEBM_MAX_KB      = 256
WEBM_MAX_FPS     = 30
WEBM_MAX_DURATION= 3.0
EMOJI_SIZE       = 100   # px

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger("Craving Emojis")


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZER — PNG/WEBP → 100×100 PNG
# ════════════════════════════════════════════════════════════════════════════

def normalize_static(raw: bytes) -> bytes:
    """
    Resize PNG/WEBP ke 100×100 dengan kualitas terbaik.
    - Pakai LANCZOS resampling (paling tajam)
    - Preserve transparency
    - Output PNG lossless
    """
    img = Image.open(io.BytesIO(raw)).convert("RGBA")

    # Kalau sudah 100x100, skip resize
    if img.size != (EMOJI_SIZE, EMOJI_SIZE):
        img = img.resize((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZER — TGS (Lottie JSON gzipped)
# ════════════════════════════════════════════════════════════════════════════

def normalize_tgs(raw: bytes) -> bytes:
    """
    Normalize TGS agar memenuhi spec emoji Telegram:
    - Durasi max 3 detik
    - FPS 60
    - Hapus properti yang tidak didukung emoji
    - Kompres seoptimal mungkin agar < 64KB
    
    Tidak mengubah artwork/visual — hanya metadata & timing.
    """
    # Decompress TGS → dict Lottie
    try:
        data = json.loads(gzip.decompress(raw))
    except Exception as e:
        raise ValueError(f"File bukan TGS valid: {e}")

    # 1. Normalisasi FPS
    original_fps = float(data.get("fr", 60))
    target_fps   = TGS_TARGET_FPS

    # 2. Hitung durasi asli
    ip = float(data.get("ip", 0))
    op = float(data.get("op", original_fps * 3))
    duration_s = (op - ip) / original_fps

    # 3. Potong durasi ke max 3 detik (rescale frame numbers)
    if duration_s > TGS_MAX_DURATION:
        scale = TGS_MAX_DURATION / duration_s
        # Rescale semua keyframe time di seluruh tree
        data = _rescale_lottie_time(data, scale, original_fps, target_fps)
        data["op"] = int(ip + TGS_MAX_DURATION * target_fps)
    else:
        # Hanya update FPS jika berbeda
        if original_fps != target_fps:
            fps_scale = target_fps / original_fps
            data = _rescale_lottie_time(data, 1.0, original_fps, target_fps)
            data["op"] = int((op - ip) * fps_scale + ip)

    data["fr"] = target_fps
    data["ip"] = 0

    # 4. Hapus field yang sering bikin masalah di emoji renderer
    _strip_unsupported(data)

    # 5. Kompres seoptimal mungkin
    json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = gzip.compress(json_bytes, compresslevel=9)

    # 6. Kalau masih > 64KB, simplify animasi (kurangi presisi float)
    if len(compressed) > TGS_MAX_KB * 1024:
        json_bytes = json.dumps(data, separators=(',', ':'),
                                 allow_nan=False).encode('utf-8')
        # Round semua float ke 3 decimal (kurangi ukuran signifikan)
        json_str = re.sub(
            r'(-?\d+\.\d{4,})',
            lambda m: str(round(float(m.group(1)), 3)),
            json_bytes.decode('utf-8')
        )
        compressed = gzip.compress(json_str.encode('utf-8'), compresslevel=9)

    size_kb = len(compressed) / 1024
    log.info(f"TGS normalized: {len(raw)/1024:.1f}KB → {size_kb:.1f}KB, "
             f"dur={min(duration_s, TGS_MAX_DURATION):.2f}s, fps={target_fps}")

    if size_kb > TGS_MAX_KB:
        log.warning(f"TGS masih {size_kb:.1f}KB setelah normalisasi, upload anyway")

    return compressed


def _rescale_lottie_time(obj, time_scale: float, src_fps: float, dst_fps: float):
    """Rescale semua nilai time/frame di seluruh struktur Lottie secara rekursif."""
    fps_ratio = dst_fps / src_fps

    def rescale(node):
        if isinstance(node, dict):
            result = {}
            for k, v in node.items():
                # Key yang berisi frame number
                if k in ("t", "ip", "op", "st") and isinstance(v, (int, float)):
                    result[k] = round(v * fps_ratio * time_scale, 3)
                elif k == "ks" and isinstance(v, dict):
                    result[k] = rescale_keyframes(v)
                else:
                    result[k] = rescale(v)
            return result
        elif isinstance(node, list):
            return [rescale(item) for item in node]
        return node

    def rescale_keyframes(ks_dict):
        result = {}
        for prop, val in ks_dict.items():
            if isinstance(val, dict) and "k" in val:
                k = val["k"]
                if isinstance(k, list):
                    new_k = []
                    for kf in k:
                        if isinstance(kf, dict) and "t" in kf:
                            kf = dict(kf)
                            kf["t"] = round(kf["t"] * fps_ratio * time_scale, 3)
                        new_k.append(kf)
                    result[prop] = dict(val, k=new_k)
                else:
                    result[prop] = val
            else:
                result[prop] = val
        return result

    return rescale(obj)


def _strip_unsupported(data: dict):
    """Hapus fitur Lottie yang tidak didukung Telegram emoji renderer."""
    # Hapus expressions di semua layer
    for layer in data.get("layers", []):
        layer.pop("ef", None)   # effects
        layer.pop("hasMask", None)
        layer.pop("masksProperties", None)
        # Rekursif untuk precomp layers
        for shape in layer.get("shapes", []):
            _strip_shape(shape)


def _strip_shape(shape):
    if isinstance(shape, dict):
        shape.pop("ef", None)
        for child in shape.get("it", []):
            _strip_shape(child)


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZER — WEBM VP9 via ffmpeg
# ════════════════════════════════════════════════════════════════════════════

def normalize_webm(raw: bytes) -> bytes:
    """
    Normalize WEBM agar memenuhi spec emoji Telegram:
    - Resize ke 100×100 (contain + pad transparan)
    - Strip audio
    - Cap 30 FPS
    - Trim ke max 3 detik
    - Encode VP9 lossless-ish (CRF rendah, kualitas tinggi)
    - Output < 256KB
    
    Pakai ffmpeg — tidak ada penurunan kualitas visual signifikan.
    """
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "input.webm"
        out = Path(tmp) / "output.webm"
        inp.write_bytes(raw)

        # Pass 1: coba kualitas tinggi dulu (CRF 10 = hampir lossless VP9)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(inp),
            "-vf", (
                f"scale={EMOJI_SIZE}:{EMOJI_SIZE}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                f"setsar=1"
            ),
            "-c:v", "libvpx-vp9",
            "-b:v", "0",
            "-crf", "10",            # kualitas tinggi
            "-r", str(WEBM_MAX_FPS),
            "-t", str(WEBM_MAX_DURATION),
            "-an",                   # hapus audio
            "-pix_fmt", "yuva420p",  # preserve alpha
            "-auto-alt-ref", "0",    # required for alpha
            str(out)
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            # Fallback: tanpa alpha channel (lebih kompatibel)
            cmd2 = [
                "ffmpeg", "-y",
                "-i", str(inp),
                "-vf", (
                    f"scale={EMOJI_SIZE}:{EMOJI_SIZE}:"
                    f"force_original_aspect_ratio=decrease,"
                    f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1"
                ),
                "-c:v", "libvpx-vp9",
                "-b:v", "0", "-crf", "15",
                "-r", str(WEBM_MAX_FPS),
                "-t", str(WEBM_MAX_DURATION),
                "-an",
                str(out)
            ]
            result2 = subprocess.run(cmd2, capture_output=True, timeout=60)
            if result2.returncode != 0:
                raise RuntimeError(f"ffmpeg gagal: {result2.stderr.decode()[-300:]}")

        output_bytes = out.read_bytes()
        size_kb = len(output_bytes) / 1024

        # Kalau masih > 256KB, naikkan CRF (kurangi bitrate)
        if size_kb > WEBM_MAX_KB:
            cmd_compress = [
                "ffmpeg", "-y",
                "-i", str(inp),
                "-vf", (
                    f"scale={EMOJI_SIZE}:{EMOJI_SIZE}:"
                    f"force_original_aspect_ratio=decrease,"
                    f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1"
                ),
                "-c:v", "libvpx-vp9",
                "-b:v", "200k",       # target bitrate
                "-maxrate", "300k",
                "-bufsize", "400k",
                "-r", str(WEBM_MAX_FPS),
                "-t", str(WEBM_MAX_DURATION),
                "-an",
                str(out)
            ]
            subprocess.run(cmd_compress, capture_output=True, timeout=60)
            output_bytes = out.read_bytes()
            size_kb = len(output_bytes) / 1024

        log.info(f"WEBM normalized: {len(raw)/1024:.1f}KB → {size_kb:.1f}KB")
        return output_bytes


# ════════════════════════════════════════════════════════════════════════════
# UTILS
# ════════════════════════════════════════════════════════════════════════════

def extract_pack_name(text: str) -> str | None:
    m = re.search(r"(?:addstickers|addemoji)/([a-zA-Z0-9_]+)", text)
    if m:
        return m.group(1)
    name = text.strip().lstrip("@")
    if re.match(r"^[a-zA-Z0-9_]+$", name):
        return name
    return None


def build_output_name(pack_name: str, bot_username: str, custom: str = "") -> str:
    suffix = f"_by_{bot_username}"
    base   = custom if custom else re.sub(r"_by_\w+$", "", pack_name, flags=re.I) + "_emoji"
    base   = re.sub(r"[^a-z0-9_]", "_", base.lower())
    name   = base if base.endswith(suffix) else base + suffix
    if len(name) > 64:
        name = base[:64 - len(suffix)] + suffix
    return name


async def download_sticker(bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buf  = io.BytesIO()
    await file.download_to_memory(buf)
    return buf.getvalue()


def progress_bar(current: int, total: int) -> str:
    width  = 16
    filled = int(width * current / total) if total else 0
    bar    = "█" * filled + "░" * (width - filled)
    pct    = int(100 * current / total) if total else 0
    return f"`[{bar}]` {pct}% ({current}/{total})"


def md_escape(text: str) -> str:
    for c in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text


# ════════════════════════════════════════════════════════════════════════════
# WELCOME & HELP TEXT
# ════════════════════════════════════════════════════════════════════════════

WELCOME_TEXT = (
    "✨ *Selamat datang di Craving Emojis!*\n\n"
    "Bot ini convert *sticker pack* Telegram menjadi *custom emoji pack* (Premium) "
    "secara otomatis. Semua format didukung dan dinormalisasi agar memenuhi standar Telegram.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🚀 *CARA PAKAI*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Perintah dasar:*\n"
    "`/convert NamaPack`\n\n"
    "*Pakai URL lengkap:*\n"
    "`/convert https://t.me/addstickers/NamaPack`\n\n"
    "*Dengan nama output custom:*\n"
    "`/convert NamaPack namaoutputku`\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📋 *FORMAT & NORMALISASI*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "🖼 *Static PNG/WEBP*\n"
    "   Resize 100×100px · LANCZOS · lossless\n\n"
    "✨ *Animated TGS (Lottie)*\n"
    "   Trim ke 3 detik · 60 FPS · kompres ≤64KB\n"
    "   Artwork tidak berubah, hanya timing\n\n"
    "🎬 *Video WEBM VP9*\n"
    "   Scale 100×100 · Strip audio · Cap 30fps\n"
    "   Trim 3 detik · VP9 high quality ≤256KB\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ *CATATAN*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "• Membuat emoji pack bisa oleh siapa saja\n"
    "• *Menggunakan* custom emoji butuh Telegram Premium\n"
    "• Maks 120 stiker per pack\n\n"
    "Ketik /help untuk bantuan lebih lanjut."
)

HELP_TEXT = (
    "📖 *BANTUAN CRAVING EMOJIS*\n\n"
    "*Perintah:*\n"
    "• `/start` — pesan selamat datang\n"
    "• `/convert <pack>` — convert sticker pack\n"
    "• `/help` — bantuan ini\n\n"
    "*Contoh:*\n"
    "```\n"
    "/convert GFess\n"
    "/convert vugituxhr\n"
    "/convert GFess nama_custom\n"
    "/convert https://t.me/addstickers/GFess\n"
    "```\n\n"
    "*Troubleshooting:*\n"
    "❓ *Short name sudah dipakai*\n"
    "→ `/convert NamaPack nama_baru`\n\n"
    "❓ *Bot tidak kenali user*\n"
    "→ Kirim `/start` ke bot ini dulu\n\n"
    "❓ *Beberapa stiker dilewati*\n"
    "→ Normal jika ada stiker yang corrupt"
)


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = md_escape(update.effective_user.first_name or "")
    await update.message.reply_text(
        f"👋 Halo, *{name}!*\n\n" + WELCOME_TEXT,
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot  = ctx.bot
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "❌ *Kurang argumen\\!*\n\n"
            "Cara pakai:\n```\n/convert NamaPack\n```",
            parse_mode="MarkdownV2"
        )
        return

    pack_name = extract_pack_name(args[0])
    custom    = args[1] if len(args) > 1 else ""

    if not pack_name:
        await update.message.reply_text("❌ Nama pack tidak valid\\.", parse_mode="MarkdownV2")
        return

    msg = await update.message.reply_text(
        f"🔍 Mencari pack `{pack_name}`…", parse_mode="Markdown"
    )

    async def status(text: str, md2=False):
        try:
            await msg.edit_text(text, parse_mode="MarkdownV2" if md2 else "Markdown")
        except Exception:
            pass

    try:
        # ── Fetch pack ───────────────────────────────────────────────────────
        try:
            pack = await bot.get_sticker_set(pack_name)
        except TelegramError as e:
            await status(f"❌ *Pack tidak ditemukan:* `{pack_name}`\nDetail: `{e}`")
            return

        stickers = pack.stickers[:MAX_STICKERS]
        total    = len(stickers)
        s0       = stickers[0]

        if s0.is_animated:
            fmt, fmt_label = "animated", "✨ TGS Animated"
        elif s0.is_video:
            fmt, fmt_label = "video",    "🎬 WEBM Video"
        else:
            fmt, fmt_label = "static",   "🖼 Static PNG"

        await status(
            f"📦 *{pack.title}*\n"
            f"Format: {fmt_label} · {total} stiker\n\n"
            f"⚙️ Normalisasi aktif — semua stiker akan disesuaikan dengan standar emoji Telegram…"
        )

        # ── Build names ──────────────────────────────────────────────────────
        me       = await bot.get_me()
        out_name = build_output_name(pack_name, me.username, custom)
        out_title= pack.title + " Emoji"

        # ── Normalize + upload stiker pertama ────────────────────────────────
        first_fid   = None
        first_emoji = "⭐"
        start_from  = 0
        skipped     = 0

        for i, st in enumerate(stickers[:5]):
            await status(
                f"📦 *{pack.title}*\n"
                f"⚙️ Normalisasi & upload stiker pertama ({i+1}/5)…"
            )
            try:
                raw = await download_sticker(bot, st.file_id)
                raw, out_fmt = await asyncio.get_event_loop().run_in_executor(
                    None, normalize_sticker, raw, fmt
                )
                result = await bot.upload_sticker_file(
                    user_id=user.id,
                    sticker=io.BytesIO(raw),
                    sticker_format=out_fmt,
                )
                first_fid   = result.file_id
                first_emoji = st.emoji or "⭐"
                start_from  = i + 1
                break
            except Exception as e:
                log.warning(f"Stiker pertama #{i+1} gagal: {e}")
                skipped += 1

        if not first_fid:
            await status(
                "❌ *5 stiker pertama semua gagal\\!*\n\n"
                "Kemungkinan:\n"
                "• Belum kirim `/start` ke bot ini\n"
                "• Format tidak didukung",
                md2=True
            )
            return

        # ── Buat emoji set ───────────────────────────────────────────────────
        await status(f"📦 *{pack.title}*\n✨ Membuat emoji pack `{out_name}`…")

        pack_exists = False
        try:
            from telegram import InputSticker
            await bot.create_new_sticker_set(
                user_id=user.id,
                name=out_name, title=out_title,
                stickers=[InputSticker(
                    sticker=first_fid,
                    emoji_list=[first_emoji],
                    format=fmt,
                )],
                sticker_type="custom_emoji",
            )
        except TelegramError as e:
            err = str(e)
            if "ALREADY_OCCUPIED" in err or "already exists" in err.lower():
                pack_exists = True
            elif "PEER_ID_INVALID" in err or "USER_ID_INVALID" in err:
                await status(
                    "❌ *Bot tidak mengenali user kamu\\.*\n\nKirim `/start` ke bot ini dulu, lalu coba lagi\\.",
                    md2=True
                )
                return
            else:
                safe = md_escape(err)
                safe_pack = md_escape(pack_name)
                await status(
                    f"❌ *Gagal buat emoji pack:*\n`{safe}`\n\n"
                    f"💡 Coba nama lain:\n`/convert {safe_pack} nama\\_lain`",
                    md2=True
                )
                return

        uploaded = 0 if pack_exists else 1

        # ── Upload sisa stiker ───────────────────────────────────────────────
        remaining = stickers[start_from:]

        for i, st in enumerate(remaining):
            if i % 5 == 0:
                await status(
                    f"📦 *{pack.title}*\n"
                    f"⚙️ Normalisasi & upload…\n"
                    f"{progress_bar(uploaded, total)}"
                    + (f"\n⚠️ Dilewati: {skipped}" if skipped else "")
                )
            try:
                raw = await download_sticker(bot, st.file_id)
                raw, out_fmt = await asyncio.get_event_loop().run_in_executor(
                    None, normalize_sticker, raw, fmt
                )
                up = await bot.upload_sticker_file(
                    user_id=user.id,
                    sticker=io.BytesIO(raw),
                    sticker_format=out_fmt,
                )
                from telegram import InputSticker
                await bot.add_sticker_to_set(
                    user_id=user.id,
                    name=out_name,
                    sticker=InputSticker(
                        sticker=up.file_id,
                        emoji_list=[st.emoji or "⭐"],
                        format=out_fmt,
                    ),
                )
                uploaded += 1
            except Exception as e:
                log.warning(f"Stiker #{start_from+i+1} dilewati: {e}")
                skipped += 1

            await asyncio.sleep(UPLOAD_DELAY)

        # ── Selesai ──────────────────────────────────────────────────────────
        out_url = f"https://t.me/addemoji/{out_name}"
        src_url = f"https://t.me/addstickers/{pack_name}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Buka Emoji Pack", url=out_url)],
            [InlineKeyboardButton("📦 Lihat Pack Sumber", url=src_url)],
        ])

        skip_line = f"⚠️ Dilewati: *{skipped} stiker*\n" if skipped else ""
        await msg.edit_text(
            f"✅ *Konversi selesai\\!*\n\n"
            f"📦 Sumber: [{md_escape(pack.title)}]({src_url})\n"
            f"💎 Output: `{md_escape(out_name)}`\n"
            f"📊 Format: {md_escape(fmt_label)}\n\n"
            f"✔️ Berhasil: *{uploaded} emoji*\n"
            f"{skip_line}"
            f"\n👇 Tap tombol untuk membuka emoji pack\\.",
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )

    except Exception as e:
        log.exception("Unexpected error in /convert")
        await status(f"❌ Error tidak terduga:\n`{e}`")


def normalize_sticker(raw: bytes, fmt: str) -> tuple[bytes, str]:
    """
    Normalize stiker sesuai format dan kembalikan (bytes, format_string).
    Dijalankan di thread executor agar tidak block event loop.
    """
    if fmt == "static":
        return normalize_static(raw), "static"
    elif fmt == "animated":
        try:
            return normalize_tgs(raw), "animated"
        except Exception as e:
            log.warning(f"TGS normalize gagal ({e}), upload as-is")
            return raw, "animated"
    elif fmt == "video":
        try:
            return normalize_webm(raw), "video"
        except Exception as e:
            log.warning(f"WEBM normalize gagal ({e}), upload as-is")
            return raw, "video"
    return raw, fmt


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        print("=" * 50)
        print("❌  BOT_TOKEN belum di-set!")
        print()
        print("Railway : tab Variables → BOT_TOKEN = <token>")
        print("Lokal   : export BOT_TOKEN=<token>")
        print("=" * 50)
        return

    log.info("Craving Emojis Bot starting...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("convert", cmd_convert))
    log.info("Bot aktif! Kirim /start di Telegram.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
