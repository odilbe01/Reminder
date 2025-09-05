# bot.py
import os
import logging
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, Dict

import asyncio
from datetime import datetime, timedelta, timezone

# ---- Optional, yaxshiroq sanani parse qilish uchun ----
try:
    from dateutil import parser as du_parser
    from dateutil import tz as du_tz
except Exception:
    du_parser = None
    du_tz = None

# ---- Python 3.9+ stdlib TZ ----
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# -----------------------------
# Config & Logging
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tripbot")

# Oxirgi ko‘rilgan Trip post (guruh bo‘yicha) — Add/Minus ishlashi uchun
CHAT_LAST_TRIP: Dict[int, str] = {}

# Rejalashtirilgan ishlar (diagnostika uchun ixtiyoriy)
SCHEDULED: Dict[Tuple[int, int], str] = {}  # (chat_id, msg_id) -> iso send time

# US TZ qisqartmalari → IANA tz
TZ_ABBR_TO_ZONE = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",
    "HDT": "Pacific/Honolulu",
    "UTC": "UTC",
    "GMT": "UTC",
}

MONTH_ABBR = {m.lower(): i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1
)}

# -----------------------------
# Utility helpers
# -----------------------------

def ascii_fold(text: str) -> str:
    """Unicode ni oddiy ASCII ga olib kelish (masalan, 𝗧𝗿𝗶𝗽 → Trip)."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if ord(ch) < 128)


def parse_first_two_dollar_amounts(text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Matndan birinchi ikkita $-summani oladi: (Rate, PerMile) tartibida."""
    amounts = re.findall(r"\$\s*([0-9][\d,]*(?:\.[0-9]{1,4})?)", text)
    if len(amounts) < 2:
        return None, None
    try:
        rate = Decimal(amounts[0].replace(",", ""))
        per_mile = Decimal(amounts[1].replace(",", ""))
        return rate, per_mile
    except Exception:
        return None, None


def parse_trip_miles(text: str) -> Optional[Decimal]:
    """'🚛 ... 431.63mi' yoki 'Trip: 431.63mi' ko‘rinishidan mileni oladi."""
    m = re.search(r"🚛[\s\S]*?(\d+[\d,]*(?:\.[0-9]{1,3})?)\s*mi\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?im)\bTrip\s*:\s*(\d+[\d,]*(?:\.[0-9]{1,3})?)\s*mi\b", ascii_fold(text))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except Exception:
        return None


def format_money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def update_rate_and_rpm_in_text(original: str, new_rate: Decimal, new_rpm: Decimal) -> Optional[str]:
    """Matndagi ikki 💰 qatoridagi summalarni yangilaydi (1-qator Rate, 2-qator /mi)."""
    lines = original.splitlines()
    rate_line_idx = None
    rpm_line_idx = None
    for idx, line in enumerate(lines):
        if "💰" in line and "$" in line:
            if "/mi" in ascii_fold(line).lower():
                if rpm_line_idx is None:
                    rpm_line_idx = idx
            else:
                if rate_line_idx is None:
                    rate_line_idx = idx

    if rate_line_idx is None or rpm_line_idx is None:
        return None

    def replace_first_dollar_amount(line: str, new_amount: str) -> str:
        return re.sub(r"\$\s*[0-9][\d,]*(?:\.[0-9]{1,4})?", new_amount, line, count=1)

    lines[rate_line_idx] = replace_first_dollar_amount(lines[rate_line_idx], format_money(new_rate))
    if "/mi" in lines[rpm_line_idx]:
        lines[rpm_line_idx] = replace_first_dollar_amount(lines[rpm_line_idx], format_money(new_rpm))
    else:
        lines[rpm_line_idx] = replace_first_dollar_amount(lines[rpm_line_idx], format_money(new_rpm) + "/mi")
    return "\n".join(lines)


# -----------------------------
# PU + offset parsing & scheduling
# -----------------------------
PU_LINE_RE = re.compile(r"(?im)^\s*PU\s*:\s*(.+?)\s*$")
OFFSET_RE = re.compile(r"(?im)^\s*(?:(?P<h>\d{1,3})\s*h)?\s*(?:(?P<m>\d{1,3})\s*m)?\s*$")


def _tz_to_zoneinfo(abbr: str):
    zone = TZ_ABBR_TO_ZONE.get(abbr.upper())
    if not zone:
        return None
    if du_tz:
        return du_tz.gettz(zone)
    if ZoneInfo:
        try:
            return ZoneInfo(zone)
        except Exception:
            return None
    return None


def parse_pu_datetime(pu_str: str) -> Optional[datetime]:
    """
    '5 Sep, 15:40 PDT' yoki 'Mon Sep 5 14:30 EDT' (ixtiyoriy yil) ko‘rinishini qabul qiladi.
    TZ qisqartmasidan timezone oladi; timezone-aware datetime qaytaradi.
    """
    s = pu_str.strip()
    tz_m = re.search(r"\b([A-Za-z]{2,4})\s*$", s)
    tzinfo = None
    if tz_m:
        tzinfo = _tz_to_zoneinfo(tz_m.group(1))

    # dateutil bo‘lsa — undan foydalanamiz
    if du_parser:
        default_year = datetime.now(timezone.utc).astimezone().year
        base = datetime(default_year, 1, 1, 0, 0, 0)
        try:
            dt = du_parser.parse(
                s,
                fuzzy=True,
                dayfirst=True,
                default=base,
                tzinfos=(lambda _name: tzinfo) if tzinfo else None,
            )
            if dt.tzinfo is None and tzinfo:
                dt = dt.replace(tzinfo=tzinfo)
            return dt
        except Exception:
            pass

    # Fallback: '5 Sep, 15:40 PDT' yoki 'Sep 5, 15:40 PDT'
    m1 = re.search(
        r"(?i)(?P<d>\d{1,2})\s+(?P<mon>[A-Za-z]{3}),?\s+(?P<h>\d{1,2}):(?P<mi>\d{2})\s+(?P<tz>[A-Za-z]{2,4})",
        s,
    )
    m2 = re.search(
        r"(?i)(?P<mon>[A-Za-z]{3})\s+(?P<d>\d{1,2}),?\s+(?P<h>\d{1,2}):(?P<mi>\d{2})\s+(?P<tz>[A-Za-z]{2,4})",
        s,
    )
    mm = m1 or m2
    if not mm:
        return None
    mon = MONTH_ABBR.get(mm.group("mon").lower())
    if not mon:
        return None
    day = int(mm.group("d"))
    hour = int(mm.group("h"))
    minute = int(mm.group("mi"))
    tz_abbr = mm.group("tz").upper()
    tzinfo = _tz_to_zoneinfo(tz_abbr) or timezone.utc
    year = datetime.now(tzinfo).year
    try:
        return datetime(year, mon, day, hour, minute, tzinfo=tzinfo)
    except Exception:
        return None


def parse_offset(text: str) -> Optional[timedelta]:
    """
    Offset satrini topadi: '1h', '1h 5m', '45m', '2h5m' va hokazo.
    Odatda PU dan keyingi satr bo‘ladi.
    """
    for line in text.splitlines():
        if "PU:" in line:
            continue
        m = OFFSET_RE.match(line.strip())
        if m and (m.group("h") or m.group("m")):
            h = int(m.group("h") or 0)
            mi = int(m.group("m") or 0)
            return timedelta(hours=h, minutes=mi)
    return None


async def schedule_ai_available_msg(
    when_dt_utc: datetime,
    chat_id: int,
    reply_to_message_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """PU − offset − 10m da reply yuborish: 'Load will be available on AI soon!'"""

    async def _job_callback(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text="Load will be available on AI soon!",
                reply_to_message_id=reply_to_message_id,
                disable_notification=True,
            )
        except Exception as e:
            logger.exception("Failed to send scheduled AI notice: %s", e)

    now_utc = datetime.now(timezone.utc)
    if when_dt_utc <= now_utc + timedelta(seconds=2):
        # O‘tib ketgan bo‘lsa — darhol yuboramiz
        await _job_callback(context)
        return

    # PTB v20: JobQueue mavjud bo‘lsa foydalanamiz
    job_queue = getattr(context, "job_queue", None) or getattr(context.application, "job_queue", None)
    if job_queue:
        try:
            job_queue.run_once(_job_callback, when=when_dt_utc)
            logger.info(
                "Scheduled AI notice at %s (UTC) for chat=%s msg=%s",
                when_dt_utc.isoformat(), chat_id, reply_to_message_id
            )
            return
        except Exception as e:
            logger.exception("Failed to schedule job_queue.run_once: %s", e)

    # Fallback: JobQueue yo‘q → asyncio sleeper (process qayta ishga tushsa saqlanmaydi)
    delay = max(0, int((when_dt_utc - now_utc).total_seconds()))

    async def _sleep_then_send():
        await asyncio.sleep(delay)
        await _job_callback(context)

    asyncio.create_task(_sleep_then_send())
    logger.warning("JobQueue missing; using asyncio fallback with delay=%ss", delay)


# -----------------------------
# Core logic: triggers & handlers
# -----------------------------
TRIP_PROMPT_1 = (
    "Please review all posted trucks—the driver is already covered. If you see a post for a covered truck, remove it.\n\n"
    "It only takes a few seconds—let’s check.\n\n"
    "@dispatchrepublic  @Aziz_157 @d1spa1ch @d1spa1ch_team"
)

TRIP_PROMPT_2 = (
    "Update team !\n\n"
    "Please ask the dispatch when you need to send the load to the driver.\n"
    "Assign Driver and Tractor.\n"
    "If there is RSRV Note that on google sheets and send it to RSRV Group.\n"
    "@usmon_offc @Alex_W911 @willliam_anderson @S1eve_21."
)


def looks_like_trip_post(text: str) -> bool:
    folded = ascii_fold(text).lower()
    return ("trip id" in folded) or ("🗺" in text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 TripBot is alive. Add me to your group and *disable privacy mode* in BotFather so I can read messages.\n\n"
        "• Reply 'Add 100' or 'Minus 100' to a Trip message to auto-recalculate Rate and $/mi.\n"
        "• When someone posts a Trip ID, I auto-reply with your two guidance messages.\n"
        "• Post like:\n"
        "  PU: 5 Sep, 15:40 PDT\n"
        "  1h 5m\n"
        "  → I will schedule a reply at PU − offset − 10 minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text

    # --- (A) PU + offset: schedule "Load will be available on AI soon!" ---
    pu_line_m = PU_LINE_RE.search(text)
    if pu_line_m:
        pu_raw = pu_line_m.group(1).strip()
        pu_dt = parse_pu_datetime(pu_raw)
        offs = parse_offset(text)
        if pu_dt and offs:
            send_at = pu_dt - offs - timedelta(minutes=10)  # doim +10 min oldin
            send_at_utc = send_at.astimezone(timezone.utc)
            try:
                await schedule_ai_available_msg(
                    when_dt_utc=send_at_utc,
                    chat_id=msg.chat_id,
                    reply_to_message_id=msg.message_id,
                    context=context,
                )
                SCHEDULED[(msg.chat_id, msg.message_id)] = send_at_utc.isoformat()
                await msg.reply_text("noted")
            except Exception as e:
                logger.exception("Failed to create schedule: %s", e)
                await msg.reply_text("⚠️ Could not schedule. Please check the PU time and offset.")
            return
        if pu_dt and not offs:
            await msg.reply_text("❗ Offset topilmadi. Keyingi qatorda '1h' yoki '1h 5m' yozing.")
            return
        if offs and not pu_dt:
            await msg.reply_text("❗ PU vaqtini parse qilib bo‘lmadi. Masalan: '5 Sep, 15:40 PDT'.")
            return

    # --- (B) Trip ID post: ikki ogohlantirishni yuborish ---
    if looks_like_trip_post(text):
        try:
            CHAT_LAST_TRIP[msg.chat_id] = text
        except Exception:
            pass
        try:
            await msg.reply_text(TRIP_PROMPT_1)
            await msg.reply_text(TRIP_PROMPT_2)
        except Exception as e:
            logger.exception("Failed to send trip prompts: %s", e)
        return

    # --- (C) 'Add X' / 'Minus X' — Rate & $/mi qayta hisoblash ---
    folded_cmd = ascii_fold(text).strip().lower()
    m = re.search(r"\b(add|minus)\s+(-?\d+(?:\.\d{1,2})?)\b", folded_cmd)
    if m:
        try:
            op = m.group(1)
            delta = Decimal(m.group(2))
            if op == "minus":
                delta = -delta

            # Reply qilingan asliga ustunlik; bo‘lmasa oxirgi Trip post
            if msg.reply_to_message and msg.reply_to_message.text:
                original_trip_text = msg.reply_to_message.text
            else:
                original_trip_text = CHAT_LAST_TRIP.get(msg.chat_id) or ""

            base_rate, _old_rpm = parse_first_two_dollar_amounts(original_trip_text)
            miles = parse_trip_miles(original_trip_text)

            if base_rate is None or miles is None or miles == 0:
                await msg.reply_text(
                    "❗ Rate/Miles topilmadi. Matnda shunga o‘xshash satrlar bo‘lsin:\n"
                    " '💰 Rate: $123.45' va '🚛 Trip: 431.63mi'",
                )
                return

            new_rate = (base_rate + delta).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            new_rpm = (new_rate / miles).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            updated_text = update_rate_and_rpm_in_text(original_trip_text, new_rate, new_rpm)
            if not updated_text:
                # Minimal fallback: 💰 blokini almashtirish
                updated_text = re.sub(
                    r"(?m)^.*💰[\s\S]*?\$[0-9][\d,]*(?:\.[0-9]{1,4})?.*$",
                    f"💰 𝗥𝗮𝘁𝗲: {format_money(new_rate)}",
                    original_trip_text,
                    count=1,
                )
                if "Per mile" in ascii_fold(updated_text):
                    updated_text = re.sub(
                        r"(?m)^.*💰[\s\S]*?\$[0-9][\d,]*(?:\.[0-9]{1,4})?.*/mi.*$",
                        f"💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi",
                        updated_text,
                        count=1,
                    )
                else:
                    parts = updated_text.splitlines()
                    insert_at = 0
                    for i, ln in enumerate(parts):
                        if "💰" in ln:
                            insert_at = i + 1
                            break
                    parts.insert(insert_at, f" 💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi")
                    updated_text = "\n".join(parts)

            await msg.reply_text(updated_text)
        except Exception as e:
            logger.exception("Failed to update rate: %s", e)
            await msg.reply_text("⚠️ Something went wrong while updating the rate.")


# -----------------------------
# Error handler (yaxshi loglar uchun)
# -----------------------------
async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=ctx.error)


# -----------------------------
# Entrypoint
# -----------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Export BOT_TOKEN=... from BotFather.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Error handler
    app.add_error_handler(_on_error)

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_text))

    logger.info("Starting TripBot...")
    # PTB v20.8
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
