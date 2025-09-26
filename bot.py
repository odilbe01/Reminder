# bot.py
import os
import re
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Set
from datetime import datetime, timedelta, timezone

# TZ backend
try:
    from dateutil import tz as du_tz
except Exception:
    du_tz = None

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# -----------------------------
# Config & Logging
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "").lower() in {"1", "true", "yes"}
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook")
PORT = int(os.getenv("PORT", "8080"))

# Agar schedule vaqti juda yaqin bo'lsa majburiy minimal kechiktirish (sekund)
MIN_DELAY_SEC = int(os.getenv("MIN_DELAY_SEC", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("schedulebot")

# -----------------------------
# Timezone mapping
# -----------------------------
TZ_ABBR_TO_ZONE = {
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MST": "America/Denver",      "MDT": "America/Denver",
    "CST": "America/Chicago",     "CDT": "America/Chicago",
    "EST": "America/New_York",    "EDT": "America/New_York",
    "AKST": "America/Anchorage",  "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",    "HDT": "Pacific/Honolulu",
    "UTC": "UTC", "GMT": "UTC",
}
MONTH_ABBR = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1
)}

def _tz_to_tzinfo(abbr: str):
    abbr = (abbr or "").upper().strip()
    zone = TZ_ABBR_TO_ZONE.get(abbr)
    if not zone:
        return None
    if du_tz:
        return du_tz.gettz(zone)
    if ZoneInfo:
        try:
            return ZoneInfo(zone)  # type: ignore
        except Exception:
            return None
    return None

# -----------------------------
# Formats we accept (strict)
# -----------------------------
# 1) Thu, Sep 25, 07:00 PM MST
RE_A = re.compile(
    r"""^(?:PU\s*:\s*)?                      # optional 'PU:'
        (?P<wkd>Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*
        (?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+
        (?P<day>\d{1,2}),\s*
        (?P<h>\d{1,2}):(?P<m>\d{2})\s*
        (?P<ampm>AM|PM)\s*
        (?P<tz>[A-Za-z]{2,4})
        $""",
    re.VERBOSE
)

# 2) 06:22 AM, 09-26-25, EDT
RE_B = re.compile(
    r"""^(?:PU\s*:\s*)?                      # optional 'PU:'
        (?P<h>\d{1,2}):(?P<m>\d{2})\s*
        (?P<ampm>AM|PM),\s*
        (?P<mm>\d{2})-(?P<dd>\d{2})-(?P<yy>\d{2}),\s*
        (?P<tz>[A-Za-z]{2,4})
        $""",
    re.VERBOSE
)

# Xabar "faqat PU" ekanini qat'iy tekshirish: bitta qatordan iborat, yuqoridagi formatlardan biri,
# boshqacha so'zlar, qo'shimcha gaplar yo'q.
def is_strict_pu_only(text: str) -> bool:
    if not text:
        return False
    # Yagona qatormi?
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if len(lines) != 1:
        return False
    line = lines[0].strip()
    return bool(RE_A.match(line) or RE_B.match(line))

def parse_pu_datetime_strict(line: str) -> Optional[datetime]:
    m = RE_A.match(line)
    if m:
        mon_abbr = m.group("mon")
        mon = MONTH_ABBR.get(mon_abbr, None)
        if not mon:
            return None
        day = int(m.group("day"))
        hour = int(m.group("h")) % 12
        minute = int(m.group("m"))
        ampm = m.group("ampm").upper()
        if ampm == "PM":
            hour += 12
            if hour == 24:  # 12:xx PM -> 12, 12:xx AM -> 0
                hour = 12
        elif ampm == "AM":
            if hour == 12:
                hour = 0
        tzinfo = _tz_to_tzinfo(m.group("tz"))
        if not tzinfo:
            return None
        # Yil: joriy yil
        now = datetime.now(tzinfo)
        year = now.year
        try:
            return datetime(year, mon, day, hour, minute, tzinfo=tzinfo)
        except Exception:
            return None

    m = RE_B.match(line)
    if m:
        hour = int(m.group("h")) % 12
        minute = int(m.group("m"))
        ampm = m.group("ampm").upper()
        if ampm == "PM":
            hour += 12
            if hour == 24:
                hour = 12
        elif ampm == "AM":
            if hour == 12:
                hour = 0
        mm = int(m.group("mm"))
        dd = int(m.group("dd"))
        yy = int(m.group("yy"))
        year = 2000 + yy  # 2 digit year -> 20YY
        tzinfo = _tz_to_tzinfo(m.group("tz"))
        if not tzinfo:
            return None
        try:
            return datetime(year, mm, dd, hour, minute, tzinfo=tzinfo)
        except Exception:
            return None

    return None

# -----------------------------
# Inline offset keyboard (multi-select)
# -----------------------------
OFFSETS = ["12h", "9h", "8h", "7h", "6h", "2h", "1h"]

OFFSET_RE = re.compile(r"^\s*(?:(?P<h>\d{1,3})\s*h)?\s*(?:(?P<m>\d{1,3})\s*m)?\s*$")

def _parse_offset_text(s: str) -> Optional[timedelta]:
    m = OFFSET_RE.match(s.strip())
    if not m:
        return None
    if not (m.group("h") or m.group("m")):
        return None
    return timedelta(hours=int(m.group("h") or 0), minutes=int(m.group("m") or 0))

def _is_future_send_time(pu_dt: datetime, offs: timedelta) -> bool:
    # -10 daqiqa talabi
    send_at_utc = (pu_dt - offs - timedelta(minutes=10)).astimezone(timezone.utc)
    return send_at_utc > datetime.now(timezone.utc)

def _available_offsets(pu_dt: datetime) -> list[str]:
    out = []
    for t in OFFSETS:
        td = _parse_offset_text(t)
        if td and _is_future_send_time(pu_dt, td):
            out.append(t)
    return out

def _btn_label(lbl: str, selected: Set[str]) -> str:
    return f"✅ {lbl}" if lbl in selected else lbl

def build_offset_keyboard(token: str, pu_dt: datetime, selected: Optional[Set[str]] = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    avail = _available_offsets(pu_dt)
    rows = []
    if avail:
        row1 = [InlineKeyboardButton(_btn_label(t, selected), callback_data=f"ofs:{token}:{t}")
                for t in avail[:4]]
        row2 = [InlineKeyboardButton(_btn_label(t, selected), callback_data=f"ofs:{token}:{t}")
                for t in avail[4:]]
        if row1:
            rows.append(row1)
        if row2:
            rows.append(row2)
    rows.append([InlineKeyboardButton("Submit", callback_data=f"ofs:{token}:submit")])
    return InlineKeyboardMarkup(rows)

# -----------------------------
# State for pending keyboards
# -----------------------------
@dataclass
class PendingCfg:
    chat_id: int
    pu_dt: datetime
    reply_to_msg_id: int
    selected: Set[str]
    keyboard_msg_id: Optional[int] = None

# token -> PendingCfg
PENDING: Dict[str, PendingCfg] = {}

def _gen_token() -> str:
    return os.urandom(6).hex()

# -----------------------------
# Scheduling helper
# -----------------------------
async def _send_ready(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, reply_to_message_id: int):
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text="PLEASE BE READY, LOAD AI TIME IS CLOSE!",
            reply_to_message_id=reply_to_message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.exception("Send scheduled message failed: %s", e)

async def schedule_message_at(
    when_dt_utc: datetime,
    chat_id: int,
    reply_to_message_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    now_utc = datetime.now(timezone.utc)
    # Minimal kechiktirish
    if MIN_DELAY_SEC > 0 and when_dt_utc <= now_utc + timedelta(seconds=MIN_DELAY_SEC):
        when_dt_utc = now_utc + timedelta(seconds=MIN_DELAY_SEC)

    # Juda yaqin → darhol
    if when_dt_utc <= now_utc + timedelta(seconds=2) and MIN_DELAY_SEC == 0:
        await _send_ready(context, chat_id, reply_to_message_id)
        return

    job_queue = getattr(context, "job_queue", None) or getattr(context.application, "job_queue", None)
    if job_queue:
        try:
            job_queue.run_once(lambda ctx: _send_ready(ctx, chat_id, reply_to_message_id), when=when_dt_utc)
            return
        except Exception as e:
            logger.exception("JobQueue.run_once failed: %s", e)

    # Fallback
    delay = max(0, int((when_dt_utc - now_utc).total_seconds()))
    async def _sleep_then_send():
        await asyncio.sleep(delay)
        await _send_ready(context, chat_id, reply_to_message_id)
    asyncio.create_task(_sleep_then_send())

async def _schedule_with_offset(pu_dt: datetime, offs: timedelta, chat_id: int, reply_to: int, ctx: ContextTypes.DEFAULT_TYPE):
    send_at_utc = (pu_dt - offs - timedelta(minutes=10)).astimezone(timezone.utc)
    await schedule_message_at(send_at_utc, chat_id, reply_to, ctx)

# -----------------------------
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Scheduler is running.\n\n"
        "Faqat *PU* yuborilganda ishlaydi. Qabul qilinadigan formatlar:\n"
        "• `Thu, Sep 25, 07:00 PM MST`\n"
        "• `06:22 AM, 09-26-25, EDT`\n\n"
        "_Qo‘shimcha gap bo‘lsa — bot javob bermaydi._",
        parse_mode=ParseMode.MARKDOWN,
    )

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Faqat PU yuborilganda (qat'iy) ishlaydi. Aks holda SKIP va HECH NIMA DEMAYDI.
    if not is_strict_pu_only(text):
        return  # absolutely silent

    # PU sanasini parse qilamiz
    line = text.splitlines()[0].strip()
    pu_dt = parse_pu_datetime_strict(line)
    if not pu_dt:
        return  # silent

    # Inline klaviatura ochamiz (multi-select). Faqat PU bo'lgani uchun ruxsat.
    token = _gen_token()
    cfg = PendingCfg(
        chat_id=msg.chat_id,
        pu_dt=pu_dt,
        reply_to_msg_id=msg.message_id,
        selected=set(),
        keyboard_msg_id=None,
    )
    PENDING[token] = cfg
    try:
        sent = await msg.reply_text(
            "Select offsets, then tap Submit:",
            reply_markup=build_offset_keyboard(token, pu_dt)
        )
        cfg.keyboard_msg_id = sent.message_id
        PENDING[token] = cfg
    except Exception as e:
        logger.exception("Failed to send keyboard: %s", e)
        # silently ignore to match "no extra responses" spirit

async def on_offset_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("ofs:"):
        return
    try:
        _p, token, choice = q.data.split(":", 2)
    except Exception:
        # Invalid action → just answer quietly
        try:
            await q.answer()
        except Exception:
            pass
        return

    cfg = PENDING.get(token)
    if not cfg:
        try:
            await q.answer("Expired.")
        except Exception:
            pass
        return

    pu_dt = cfg.pu_dt
    selected = cfg.selected

    if choice == "submit":
        # faqat kelajakdagilarni qoldiramiz va schedule qilamiz
        valid = []
        for off_txt in selected:
            td = _parse_offset_text(off_txt)
            if td and _is_future_send_time(pu_dt, td):
                valid.append(off_txt)

        if not valid:
            # Klaviaturani yangilab, mavjud bo'lmaganlarni olib tashlaymiz
            try:
                avail = set(_available_offsets(pu_dt))
                cfg.selected = selected & avail
                await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, cfg.selected))
                await q.answer("No valid options.")
            except Exception:
                pass
            return

        # Schedule barchasini
        try:
            for off_txt in sorted(valid, key=lambda s: int(re.match(r"(\d+)", s).group(1)), reverse=True):
                td = _parse_offset_text(off_txt)
                if td:
                    await _schedule_with_offset(pu_dt, td, cfg.chat_id, cfg.reply_to_msg_id, context)
        except Exception as e:
            logger.exception("Failed to schedule: %s", e)
            # tozalaymiz va jim qolamiz
            PENDING.pop(token, None)
            try:
                await q.edit_message_reply_markup(reply_markup=None)
                await q.answer()
            except Exception:
                pass
            return

        # Muvaffaqiyat — klaviaturani yopamiz, qisqa tasdiq (lekin talab jim bo‘lish shart emas)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.answer("Scheduled.")
        except Exception:
            pass
        PENDING.pop(token, None)
        return

    # Toggle offset
    if choice in OFFSETS:
        td = _parse_offset_text(choice)
        if not td or not _is_future_send_time(pu_dt, td):
            # O'tmish bo'lsa: variantni UI dan olib tashlashga harakat
            try:
                avail = set(_available_offsets(pu_dt))
                cfg.selected = selected & avail
                await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, cfg.selected))
                await q.answer("This option is already in the past.")
            except Exception:
                pass
            return

        if choice in selected:
            selected.remove(choice)
        else:
            selected.add(choice)
        cfg.selected = selected
        try:
            await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, selected))
            await q.answer("Toggled.")
        except Exception as e:
            logger.exception("Failed to refresh keyboard: %s", e)
        return

    # Unknown choice
    try:
        await q.answer()
    except Exception:
        pass

# -----------------------------
# Error handler
# -----------------------------
async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=ctx.error)

# -----------------------------
# Entrypoint
# -----------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_error_handler(_on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_offset_button, pattern=r"^ofs:"))
    # Faqat text/caption; komandalar emas
    app.add_handler(MessageHandler(~filters.COMMAND & (filters.TEXT | filters.CAPTION), on_message))

    logger.info("Starting Scheduler Bot...")

    if USE_WEBHOOK and WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True, close_loop=False)

if __name__ == "__main__":
    main()
