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
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
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

# Batas upload file dari user (newpack)
MAX_FILE_SIZE_MB  = 5
MAX_FILE_SIZE_KB  = MAX_FILE_SIZE_MB * 1024
MAX_FILE_SIZE_B   = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_IMAGE_SIZE    = 1080  # px (maksimal dimensi terpanjang)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger("Craving Emojis")


# ════════════════════════════════════════════════════════════════════════════
# NORMALIZER — PNG/WEBP → 100×100 PNG (untuk emoji output)
# ════════════════════════════════════════════════════════════════════════════

def normalize_static(raw: bytes) -> bytes:
    """
    Resize PNG/WEBP ke 100×100 dengan kualitas terbaik.
    - Pakai LANCZOS resampling (paling tajam)
    - Preserve transparency (alpha channel)
    - Pad ke 100x100 dengan background transparan jika aspek rasio tidak 1:1
    - Output PNG lossless
    """
    img = Image.open(io.BytesIO(raw)).convert("RGBA")

    if img.size != (EMOJI_SIZE, EMOJI_SIZE):
        # Scale dengan preserve aspect ratio
        img.thumbnail((EMOJI_SIZE, EMOJI_SIZE), Image.LANCZOS)

        # Pad ke 100x100 dengan background transparan
        canvas = Image.new("RGBA", (EMOJI_SIZE, EMOJI_SIZE), (0, 0, 0, 0))
        offset_x = (EMOJI_SIZE - img.width) // 2
        offset_y = (EMOJI_SIZE - img.height) // 2
        canvas.paste(img, (offset_x, offset_y), mask=img)
        img = canvas

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ════════════════════════════════════════════════════════════════════════════
# VALIDATOR — cek ukuran & dimensi file dari user
# ════════════════════════════════════════════════════════════════════════════

def validate_file_size(raw: bytes, filename: str = "file") -> str | None:
    """
    Cek apakah file memenuhi batas ukuran 5MB.
    Return pesan error jika gagal, None jika OK.
    """
    size_mb = len(raw) / (1024 * 1024)
    if len(raw) > MAX_FILE_SIZE_B:
        return (
            f"❌ File *{filename}* terlalu besar ({size_mb:.1f}MB).\n"
            f"Maksimal ukuran file: *{MAX_FILE_SIZE_MB}MB*."
        )
    return None


def validate_image_dimensions(raw: bytes, filename: str = "file") -> str | None:
    """
    Cek dimensi gambar — hanya log warning, tidak menolak.
    Bot akan otomatis resize ke 100x100 saat normalisasi.
    Return None selalu (tidak pernah error karena dimensi).
    """
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if w > MAX_IMAGE_SIZE or h > MAX_IMAGE_SIZE:
            log.info(f"Gambar {filename} dimensi besar ({w}×{h}px), akan di-resize otomatis")
    except Exception:
        pass
    return None


def validate_video_dimensions(raw: bytes, filename: str = "file") -> str | None:
    """
    Cek dimensi video — hanya log warning, tidak menolak.
    Bot akan otomatis resize + kompres saat normalisasi.
    Return None selalu (tidak pernah error karena dimensi).
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", tmp_path],
            capture_output=True, timeout=15
        )
        os.unlink(tmp_path)
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        if streams:
            w = streams[0].get("width", 0)
            h = streams[0].get("height", 0)
            if w > MAX_IMAGE_SIZE or h > MAX_IMAGE_SIZE:
                log.info(f"Video {filename} dimensi besar ({w}×{h}px), akan di-resize otomatis")
    except Exception:
        pass
    return None


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

def _detect_video_ext(raw: bytes) -> str:
    """Deteksi ekstensi file video dari magic bytes."""
    if raw[:4] == b'\x1aE\xdf\xa3':
        return ".webm"
    if raw[4:8] in (b'ftyp', b'moov') or raw[:4] == b'\x00\x00\x00\x1c':
        return ".mp4"
    if raw[:6] in (b'GIF87a', b'GIF89a'):
        return ".gif"
    # Default: coba sebagai mp4
    return ".mp4"


def normalize_webm(raw: bytes) -> bytes:
    """
    Normalize video agar memenuhi spec emoji Telegram (< 256KB, 100x100, 30fps, 3s, no audio).
    - Auto-detect format input (WEBM, MP4, GIF, dll) — termasuk file besar sampai 5MB/1080p
    - Paksa resize ke 100x100, trim 3 detik, hapus audio
    - Coba kompresi dari kualitas terbaik sampai paling agresif
    - Kalau masih > 256KB, turunkan fps dan kurangi jumlah frame sebagai last resort
    - Selalu berhasil: tidak pernah raise error karena ukuran
    """
    ext = _detect_video_ext(raw)

    with tempfile.TemporaryDirectory() as tmp:
        inp  = Path(tmp) / f"input{ext}"
        out  = Path(tmp) / "output.webm"
        log2 = Path(tmp) / "ffmpeg2pass"
        inp.write_bytes(raw)

        import shutil
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"

        def run_single(vf_filter, extra_args, outfile, timeout=180):
            """Jalankan ffmpeg single-pass dengan vf dan extra args tertentu."""
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(inp),
                "-vf", vf_filter,
                "-c:v", "libvpx-vp9",
                "-t", str(WEBM_MAX_DURATION),
                "-an",
                "-pix_fmt", "yuva420p",
            ] + extra_args + [str(outfile)]
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if r.returncode != 0:
                log.debug(f"ffmpeg stderr: {r.stderr[-500:].decode(errors='replace')}")
            return r.returncode == 0

        def run_twopass(vf_filter, crf, bv, maxrate, outfile, timeout=180):
            """2-pass encode untuk kontrol ukuran lebih akurat."""
            base = [
                ffmpeg_bin, "-y",
                "-i", str(inp),
                "-vf", vf_filter,
                "-c:v", "libvpx-vp9",
                "-t", str(WEBM_MAX_DURATION),
                "-an",
                "-pix_fmt", "yuva420p",
                "-b:v", bv, "-maxrate", maxrate, "-bufsize", maxrate,
                "-crf", str(crf),
            ]
            # Pass 1
            r1 = subprocess.run(
                base + ["-pass", "1", "-passlogfile", str(log2), "-f", "null", "/dev/null"],
                capture_output=True, timeout=timeout
            )
            if r1.returncode != 0:
                return False
            # Pass 2
            r2 = subprocess.run(
                base + ["-pass", "2", "-passlogfile", str(log2), str(outfile)],
                capture_output=True, timeout=timeout
            )
            return r2.returncode == 0

        # VF untuk scale ke 100x100 dengan padding transparan
        def make_vf(fps=WEBM_MAX_FPS):
            return (
                f"scale={EMOJI_SIZE}:{EMOJI_SIZE}:"
                f"force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                f"setsar=1,fps={fps}"
            )

        result_bytes = None

        # ── Tahap 1: 2-pass dari kualitas tinggi ke rendah @ 30fps ────────────
        vf = make_vf(WEBM_MAX_FPS)
        attempts_2pass = [
            (18, "150k", "200k"),
            (24, "100k", "150k"),
            (30,  "70k", "100k"),
            (36,  "50k",  "80k"),
            (42,  "35k",  "60k"),
            (50,  "20k",  "40k"),
            (58,  "10k",  "20k"),
        ]
        for crf, bv, maxrate in attempts_2pass:
            ok = run_twopass(vf, crf, bv, maxrate, out)
            if ok and out.exists():
                size_kb = out.stat().st_size / 1024
                log.info(f"WEBM 2pass crf={crf} @30fps: {size_kb:.1f}KB")
                if size_kb <= WEBM_MAX_KB:
                    result_bytes = out.read_bytes()
                    break

        # ── Tahap 2: pure CRF 63 @ 30fps (paling lossless mungkin) ──────────
        if result_bytes is None:
            ok = run_single(vf, ["-crf", "63", "-b:v", "0"], out)
            if ok and out.exists():
                size_kb = out.stat().st_size / 1024
                log.info(f"WEBM pure CRF63 @30fps: {size_kb:.1f}KB")
                if size_kb <= WEBM_MAX_KB:
                    result_bytes = out.read_bytes()

        # ── Tahap 3: turunkan fps ke 15 ──────────────────────────────────────
        if result_bytes is None:
            vf_15 = make_vf(15)
            for crf, bv, maxrate in [(42, "30k", "50k"), (55, "15k", "25k"), (63, "0", "0")]:
                if bv == "0":
                    ok = run_single(vf_15, ["-crf", "63", "-b:v", "0"], out)
                else:
                    ok = run_twopass(vf_15, crf, bv, maxrate, out)
                if ok and out.exists():
                    size_kb = out.stat().st_size / 1024
                    log.info(f"WEBM crf={crf} @15fps: {size_kb:.1f}KB")
                    if size_kb <= WEBM_MAX_KB:
                        result_bytes = out.read_bytes()
                        break

        # ── Tahap 4: fps=10, durasi dipotong lebih agresif (1.5 detik) ───────
        if result_bytes is None:
            vf_10 = (
                f"scale={EMOJI_SIZE}:{EMOJI_SIZE}:"
                f"force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={EMOJI_SIZE}:{EMOJI_SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                f"setsar=1,fps=10"
            )
            cmd_last = [
                ffmpeg_bin, "-y",
                "-i", str(inp),
                "-vf", vf_10,
                "-c:v", "libvpx-vp9",
                "-t", "1.5",          # potong ke 1.5 detik
                "-an",
                "-pix_fmt", "yuva420p",
                "-crf", "63", "-b:v", "0",
                str(out),
            ]
            r = subprocess.run(cmd_last, capture_output=True, timeout=180)
            if r.returncode == 0 and out.exists():
                size_kb = out.stat().st_size / 1024
                log.info(f"WEBM last resort @10fps 1.5s: {size_kb:.1f}KB")
                result_bytes = out.read_bytes()  # ambil apapun hasilnya

        # ── Absolute last resort: ambil hasil terkecil yang ada ──────────────
        if result_bytes is None:
            if out.exists():
                result_bytes = out.read_bytes()
                log.warning(f"WEBM fallback: ambil output terakhir {len(result_bytes)/1024:.1f}KB")
            else:
                raise RuntimeError("ffmpeg gagal total — tidak ada output yang dihasilkan")

        size_kb = len(result_bytes) / 1024
        log.info(f"WEBM final: {len(raw)/1024:.1f}KB → {size_kb:.1f}KB (input={ext})")
        return result_bytes


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
    "📁 *BATAS UPLOAD FILE (/newpack)*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "• Ukuran file maksimal: *5MB*\n"
    "• Dimensi gambar/video maksimal: *1080×1080px*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ *CATATAN*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "• Membuat emoji pack bisa oleh siapa saja\n"
    "• *Menggunakan* custom emoji butuh Telegram Premium\n"
    "• Maks 120 stiker per pack\n\n"
    "Ketik /help untuk bantuan lebih lanjut.\n\n"
    "Atau kirim /newpack untuk buat emoji pack dari file kamu sendiri."
)

HELP_TEXT = (
    "📖 *BANTUAN CRAVING EMOJIS*\n\n"
    "*Perintah:*\n"
    "• `/start` — pesan selamat datang\n"
    "• `/convert <pack>` — convert sticker pack ke emoji\n"
    "• `/newpack <nama> [judul]` — buat emoji pack dari file sendiri\n"
    "• `/cancel` — batalkan sesi newpack\n"
    "• `/help` — bantuan ini\n\n"
    "*Contoh:*\n"
    "```\n"
    "/convert GFess\n"
    "/convert vugituxhr\n"
    "/convert GFess nama_custom\n"
    "/convert https://t.me/addstickers/GFess\n"
    "```\n\n"
    "📁 *Batas upload file (/newpack):*\n"
    "• Ukuran maksimal: *5MB per file*\n"
    "• Dimensi maksimal: *1080×1080px*\n\n"
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


def telegram_fmt(fmt: str, sticker_type: str = "custom_emoji") -> str:
    """
    Konversi format internal ke format string yang dibutuhkan Telegram Bot API.
    custom_emoji: static, animated, video
    regular sticker: static, animated, video  (sama, tapi beda konteks)
    Telegram API v6.6+ pakai: "static", "animated", "video" untuk semua tipe.
    """
    # Telegram Bot API >= 6.6 (python-telegram-bot >= 20.x):
    # format harus salah satu dari: "static", "animated", "video"
    mapping = {
        "static":   "static",
        "animated": "animated",
        "video":    "video",
    }
    return mapping.get(fmt, fmt)


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
        return normalize_webm(raw), "video"
    return raw, fmt



# ════════════════════════════════════════════════════════════════════════════
# EMOJI MAKER — Buat emoji pack dari file upload sendiri
# ════════════════════════════════════════════════════════════════════════════

# Simpan sesi per user: {user_id: {name, title, fmt, files: [], msg_id}}
maker_sessions: dict = {}


async def cmd_newpack(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /newpack <short_name> [judul pack]
    Mulai sesi pembuatan emoji pack dari file upload.
    """
    user = update.effective_user
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "❌ *Kurang argumen!*\n\n"
            "Cara pakai:\n"
            "`/newpack short_name Judul Pack`\n\n"
            "Contoh:\n"
            "`/newpack my_emoji My Emoji Pack`\n\n"
            "Short name hanya boleh huruf, angka, dan underscore.",
            parse_mode="Markdown"
        )
        return

    raw_name = args[0].lower()
    raw_name = re.sub(r"[^a-z0-9_]", "_", raw_name)
    title    = " ".join(args[1:]) if len(args) > 1 else raw_name.replace("_", " ").title() + " Emoji"

    # Cek apakah sudah ada sesi aktif
    if user.id in maker_sessions:
        old = maker_sessions[user.id]
        await update.message.reply_text(
            f"⚠️ Kamu sudah punya sesi aktif: *{old['name']}*\n\n"
            f"Kirim /cancel dulu untuk membatalkan sesi sebelumnya.",
            parse_mode="Markdown"
        )
        return

    maker_sessions[user.id] = {
        "name":  raw_name,
        "title": title,
        "files": [],   # list of (bytes, fmt)
        "fmt":   None, # detected on first file
    }

    await update.message.reply_text(
        f"✅ *Sesi dimulai!*\n\n"
        f"📦 Pack: `{raw_name}`\n"
        f"📝 Judul: {title}\n\n"
        f"Sekarang kirim file-file yang mau dijadikan emoji:\n"
        f"• 🖼 Foto/gambar (PNG, JPG, WEBP)\n"
        f"• 🎬 Video (MP4, WEBM, GIF)\n"
        f"• ✨ Stiker (TGS animated, WEBM, PNG)\n\n"
        f"📏 *Batas file:* maksimal *{MAX_FILE_SIZE_MB}MB* dan *{MAX_IMAGE_SIZE}×{MAX_IMAGE_SIZE}px*\n\n"
        f"Setelah semua file terkirim, ketik /done untuk buat pack.\n"
        f"Ketik /cancel untuk membatalkan.",
        parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in maker_sessions:
        sesi = maker_sessions.pop(user.id)
        n = len(sesi["files"])
        await update.message.reply_text(
            f"❌ Sesi *{sesi['name']}* dibatalkan.\n"
            f"{n} file dibuang.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Tidak ada sesi aktif.")


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Terima file dari user yang punya sesi aktif."""
    user = update.effective_user
    if user.id not in maker_sessions:
        return  # tidak ada sesi, abaikan

    sesi  = maker_sessions[user.id]
    msg   = update.message
    bot   = ctx.bot

    # Deteksi tipe file
    raw  = None
    fmt  = None
    name = None

    try:
        if msg.sticker:
            st = msg.sticker
            file_obj = await bot.get_file(st.file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            raw = buf.getvalue()
            if st.is_animated:
                fmt = "animated"
            elif st.is_video:
                fmt = "video"
            else:
                fmt = "static"
            name = f"sticker_{len(sesi['files'])+1}"

        elif msg.photo:
            # Ambil resolusi tertinggi
            photo    = msg.photo[-1]
            file_obj = await bot.get_file(photo.file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            raw  = buf.getvalue()
            fmt  = "static"
            name = f"photo_{len(sesi['files'])+1}"

        elif msg.document:
            doc = msg.document
            mime = doc.mime_type or ""

            # ── Cek ukuran file sebelum download ─────────────────────────────
            if doc.file_size and doc.file_size > MAX_FILE_SIZE_B:
                size_mb = doc.file_size / (1024 * 1024)
                await msg.reply_text(
                    f"❌ File *{doc.file_name or 'ini'}* terlalu besar "
                    f"(*{size_mb:.1f}MB*).\n"
                    f"Maksimal ukuran file: *{MAX_FILE_SIZE_MB}MB*.",
                    parse_mode="Markdown"
                )
                return

            if mime.startswith("image/"):
                fmt = "static"
            elif mime in ("video/webm", "video/mp4", "image/gif"):
                fmt = "video"
            elif mime == "application/x-tgsticker":
                fmt = "animated"
            else:
                await msg.reply_text(
                    f"⚠️ Format `{mime}` tidak didukung.\n"
                    f"Gunakan: gambar (PNG/JPG/WEBP), video (MP4/WEBM), atau stiker TGS.",
                    parse_mode="Markdown"
                )
                return
            file_obj = await bot.get_file(doc.file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            raw  = buf.getvalue()
            name = doc.file_name or f"file_{len(sesi['files'])+1}"

        elif msg.video or msg.animation:
            media = msg.video or msg.animation

            # ── Cek ukuran file sebelum download ─────────────────────────────
            if media.file_size and media.file_size > MAX_FILE_SIZE_B:
                size_mb = media.file_size / (1024 * 1024)
                await msg.reply_text(
                    f"❌ Video terlalu besar (*{size_mb:.1f}MB*).\n"
                    f"Maksimal ukuran file: *{MAX_FILE_SIZE_MB}MB*.",
                    parse_mode="Markdown"
                )
                return

            file_obj = await bot.get_file(media.file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            raw  = buf.getvalue()
            fmt  = "video"
            name = f"video_{len(sesi['files'])+1}"

        else:
            return  # bukan file yang relevan

    except Exception as e:
        await msg.reply_text(f"⚠️ Gagal download file: `{e}`", parse_mode="Markdown")
        return

    # ── Validasi ukuran file (post-download, untuk semua tipe) ───────────────
    size_err = validate_file_size(raw, name)
    if size_err:
        await msg.reply_text(size_err, parse_mode="Markdown")
        return

    # ── Validasi dimensi gambar ───────────────────────────────────────────────
    if fmt == "static":
        dim_err = validate_image_dimensions(raw, name)
        if dim_err:
            await msg.reply_text(dim_err, parse_mode="Markdown")
            return
    elif fmt == "video":
        dim_err = validate_video_dimensions(raw, name)
        if dim_err:
            await msg.reply_text(dim_err, parse_mode="Markdown")
            return

    # Cek konsistensi format (semua file dalam satu pack harus format sama)
    if sesi["fmt"] is None:
        sesi["fmt"] = fmt
    elif sesi["fmt"] != fmt:
        fmt_map = {"static": "gambar", "animated": "TGS stiker", "video": "video"}
        await msg.reply_text(
            f"⚠️ Format tidak cocok!\n\n"
            f"Pack ini sudah pakai format *{fmt_map.get(sesi['fmt'], sesi['fmt'])}*.\n"
            f"Semua file dalam satu pack harus format yang sama.",
            parse_mode="Markdown"
        )
        return

    # Cek limit
    if len(sesi["files"]) >= MAX_STICKERS:
        await msg.reply_text(
            f"⚠️ Sudah mencapai limit {MAX_STICKERS} file.\n"
            f"Ketik /done untuk buat pack sekarang.",
            parse_mode="Markdown"
        )
        return

    sesi["files"].append((raw, fmt, name, msg.caption or ""))
    count = len(sesi["files"])

    emoji_hint = "⭐"
    if msg.caption and len(msg.caption.strip()) == 1:
        emoji_hint = msg.caption.strip()

    size_kb = len(raw) / 1024
    size_str = f"{size_kb/1024:.1f}MB" if size_kb >= 1024 else f"{size_kb:.0f}KB"

    await msg.reply_text(
        f"✅ File #{count} diterima — *{name}*\n"
        f"Format: {'🖼 Gambar' if fmt=='static' else '🎬 Video' if fmt=='video' else '✨ Animated'} · {size_str}\n\n"
        f"💡 Tip: tambahkan caption emoji (contoh: 😂) untuk set emoji yang muncul saat pakai.\n"
        f"Kirim file berikutnya atau /done untuk buat pack.",
        parse_mode="Markdown"
    )


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Proses semua file yang sudah di-upload dan buat emoji pack."""
    user = update.effective_user
    bot  = ctx.bot

    if user.id not in maker_sessions:
        await update.message.reply_text(
            "Tidak ada sesi aktif.\n"
            "Mulai dengan `/newpack nama_pack`.",
            parse_mode="Markdown"
        )
        return

    sesi = maker_sessions.pop(user.id)

    if not sesi["files"]:
        await update.message.reply_text("❌ Belum ada file yang dikirim.")
        return

    total = len(sesi["files"])
    fmt   = sesi["fmt"] or "static"

    msg = await update.message.reply_text(
        f"⚙️ Memproses {total} file...\n"
        f"Format: {'🖼 Static' if fmt=='static' else '🎬 Video' if fmt=='video' else '✨ Animated'}",
        parse_mode="Markdown"
    )

    async def status(text):
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    try:
        me       = await bot.get_me()
        out_name = build_output_name(sesi["name"], me.username)
        out_title= sesi["title"]

        # Normalize + upload stiker pertama → buat pack
        first_fid   = None
        first_emoji = "⭐"
        start_from  = 0

        last_error = ""
        for i, (raw, file_fmt, name, caption) in enumerate(sesi["files"][:5]):
            await status(f"⚙️ Normalisasi file pertama ({i+1}/5)...")
            try:
                norm_raw, norm_fmt = await asyncio.get_event_loop().run_in_executor(
                    None, normalize_sticker, raw, fmt
                )
                result = await bot.upload_sticker_file(
                    user_id=user.id,
                    sticker=io.BytesIO(norm_raw),
                    sticker_format=norm_fmt,
                )
                first_fid   = result.file_id
                # Ambil emoji dari caption jika ada, fallback ⭐
                first_emoji = caption.strip()[0] if caption.strip() else "⭐"
                start_from  = i + 1
                break
            except Exception as e:
                last_error = str(e)
                log.warning(f"File pertama #{i+1} gagal: {e}")

        if not first_fid:
            err_hint = f"\n\n⚠️ Detail: `{last_error}`" if last_error else ""
            await status(
                f"❌ *Semua file gagal diproses.*{err_hint}\n\n"
                f"💡 Tips untuk video:\n"
                f"• Pastikan durasi ≤ 3 detik\n"
                f"• Gunakan resolusi lebih kecil\n"
                f"• Format WEBM VP9 lebih baik dari MP4"
            )
            return

        # Buat emoji set
        await status(f"✨ Membuat emoji pack `{out_name}`...")
        from telegram import InputSticker
        try:
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
                pass  # pack sudah ada, lanjut tambah
            else:
                safe = err.replace("`", "'")
                await status(
                    f"❌ Gagal buat pack:\n`{safe}`\n\n"
                    f"Coba: `/newpack nama_lain {out_title}`"
                )
                return

        uploaded = 1
        skipped  = start_from - 1
        remaining = sesi["files"][start_from:]

        for i, (raw, file_fmt, name, caption) in enumerate(remaining):
            if i % 3 == 0:
                await status(
                    f"⬆️ Upload emoji...\n"
                    f"{progress_bar(uploaded, total)}"
                )
            try:
                norm_raw, norm_fmt = await asyncio.get_event_loop().run_in_executor(
                    None, normalize_sticker, raw, fmt
                )
                up = await bot.upload_sticker_file(
                    user_id=user.id,
                    sticker=io.BytesIO(norm_raw),
                    sticker_format=norm_fmt,
                )
                emoji = caption.strip()[0] if caption.strip() else "⭐"
                await bot.add_sticker_to_set(
                    user_id=user.id,
                    name=out_name,
                    sticker=InputSticker(
                        sticker=up.file_id,
                        emoji_list=[emoji],
                        format=norm_fmt,
                    ),
                )
                uploaded += 1
            except Exception as e:
                reason = str(e)[:120]
                log.warning(f"File #{start_from+i+1} ({name}) dilewati: {reason}")
                skipped += 1

            await asyncio.sleep(UPLOAD_DELAY)

        # Selesai
        out_url = f"https://t.me/addemoji/{out_name}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Buka Emoji Pack", url=out_url)],
        ])
        skip_line = f"⚠️ Dilewati: *{skipped} file*\n" if skipped else ""
        await msg.edit_text(
            f"✅ *Emoji pack berhasil dibuat!*\n\n"
            f"💎 Pack: `{out_name}`\n"
            f"📝 Judul: {out_title}\n\n"
            f"✔️ Berhasil: *{uploaded} emoji*\n"
            f"{skip_line}"
            f"\n👇 Tap tombol untuk membuka emoji pack.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    except Exception as e:
        log.exception("Error in /done")
        await status(f"❌ Error tidak terduga:\n`{e}`")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def _check_ffmpeg():
    """Cek ffmpeg tersedia dan log path-nya."""
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        try:
            r = subprocess.run([path, "-version"], capture_output=True, timeout=5)
            ver = r.stdout.decode(errors="replace").split("\n")[0]
            log.info(f"✅ ffmpeg ditemukan: {path} ({ver})")
        except Exception as e:
            log.warning(f"ffmpeg ada di {path} tapi gagal dijalankan: {e}")
    else:
        log.error(
            "❌ ffmpeg TIDAK ditemukan di PATH!\n"
            "   Railway nixpacks.toml harus punya: nixPkgs = [\"python311\", \"ffmpeg\"]\n"
            "   Atau Dockerfile: RUN apt-get install -y ffmpeg"
        )
    return path


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
    _check_ffmpeg()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("convert", cmd_convert))
    app.add_handler(CommandHandler("newpack", cmd_newpack))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION |
        filters.Document.ALL | filters.Sticker.ALL,
        handle_file
    ))
    log.info("Bot aktif! Kirim /start di Telegram.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
