# bot.py
import os
import logging
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, Dict, Set, List

import asyncio
from datetime import datetime, timedelta, timezone

# Kuchli sana-parsing (ixtiyoriy, bo'lsa ishlatamiz)
try:
    from dateutil import parser as du_parser
    from dateutil import tz as du_tz
except Exception:
    du_parser = None
    du_tz = None

# Stdlib TZ
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# -----------------------------
# Config & Logging
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "").lower() in {"1", "true", "yes"}
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook")
PORT = int(os.getenv("PORT", "8080"))

# Agar o‘tib ketgan bo‘lsa ham eng kam kechikish (sekund) — 0 qilsa darhol yuboradi
MIN_DELAY_SEC = int(os.getenv("MIN_DELAY_SEC", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tripbot")

CHAT_LAST_TRIP: Dict[int, str] = {}
SCHEDULED: Dict[Tuple[int, int], str] = {}

# (NEW) Pending schedule holati: tanlangan offsetlar va PU vaqti
PENDING_PU: Dict[Tuple[int, int], datetime] = {}          # (chat_id, origin_msg_id) -> PU dt (tz-aware)
PENDING_OFFSETS: Dict[Tuple[int, int], Set[int]] = {}     # (chat_id, origin_msg_id) -> {12, 9, ...}

TZ_ABBR_TO_ZONE = {
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MST": "America/Denver",      "MDT": "America/Denver",
    "CST": "America/Chicago",     "CDT": "America/Chicago",
    "EST": "America/New_York",    "EDT": "America/New_York",
    "AKST": "America/Anchorage",  "AKDT": "America/Anchorage",
    "HST": "Pacific/Honolulu",    "HDT": "Pacific/Honolulu",
    "UTC": "UTC", "GMT": "UTC",
}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1
)}

# -----------------------------
# Helpers
# -----------------------------
def ascii_fold(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if ord(ch) < 128)

def parse_first_two_dollar_amounts(text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    amounts = re.findall(r"\$\s*([0-9][\d,]*(?:\.[0-9]{1,4})?)", text)
    try:
        a0 = Decimal(amounts[0].replace(",", "")) if len(amounts) >= 1 else None
        a1 = Decimal(amounts[1].replace(",", "")) if len(amounts) >= 2 else None
        return a0, a1
    except Exception:
        return None, None

def parse_first_dollar_amount(text: str) -> Optional[Decimal]:
    m = re.search(r"\$\s*([0-9][\d,]*(?:\.[0-9]{1,4})?)", text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except Exception:
        return None

def parse_trip_miles(text: str) -> Optional[Decimal]:
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
    if rate_line_idx is None:
        return None

    def repl(line: str, amt: str) -> str:
        return re.sub(r"\$\s*[0-9][\d,]*(?:\.[0-9]{1,4})?", amt, line, count=1)

    lines[rate_line_idx] = repl(lines[rate_line_idx], format_money(new_rate))

    # RPM qatori bo'lmasa ham qo'shib yuboramiz
    if rpm_line_idx is not None:
        if "/mi" in lines[rpm_line_idx]:
            lines[rpm_line_idx] = repl(lines[rpm_line_idx], format_money(new_rpm))
        else:
            lines[rpm_line_idx] = repl(lines[rpm_line_idx], format_money(new_rpm) + "/mi")
    else:
        # Birinchi 💰 qatordan keyin RPM ni qo'shamiz
        insert_at = rate_line_idx + 1
        lines.insert(insert_at, f" 💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi")

    return "\n".join(lines)

# =============================
# FLEX: 1 yoki 2 qatorda (faqat sonlar) — $ ixtiyoriy, /mi ixtiyoriy
# =============================
RPM_DECISION_MAX = Decimal("25")  # bitta son bo‘lsa: <=25 -> RPM deb qabul qilamiz

ANY_AMOUNT_RE = re.compile(r"^\s*\$?\s*([0-9][\d,]*(?:\.[0-9]{1,4})?)\s*(?:/mi)?\s*$", re.IGNORECASE)
HAS_PER_MI_RE = re.compile(r"/\s*mi\b", re.IGNORECASE)

def _parse_flex_rate_rpm(text: str) -> Optional[Tuple[Optional[Decimal], Optional[Decimal]]]:
    raw_lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in raw_lines if ln != ""]
    if len(lines) == 0 or len(lines) > 2:
        return None
    if any(not ANY_AMOUNT_RE.match(ln) for ln in lines):
        return None

    def to_dec(s: str) -> Decimal:
        m = re.search(r"([0-9][\d,]*(?:\.[0-9]{1,4})?)", s)
        return Decimal(m.group(1).replace(",", ""))  # type: ignore

    if len(lines) == 2:
        l1, l2 = lines
        l1_permi = bool(HAS_PER_MI_RE.search(l1))
        l2_permi = bool(HAS_PER_MI_RE.search(l2))
        v1, v2 = to_dec(l1), to_dec(l2)
        if l1_permi and not l2_permi:
            return (v2, v1)  # (rate, rpm)
        if l2_permi and not l1_permi:
            return (v1, v2)
        return (v1, v2)

    l = lines[0]
    v = to_dec(l)
    if HAS_PER_MI_RE.search(l):
        return (None, v)
    if v <= RPM_DECISION_MAX:
        return (None, v)
    else:
        return (v, None)

def _fmt_money(dec: Decimal) -> str:
    return f"${dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"

def _fmt_rpm(dec: Decimal) -> str:
    return f"${dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"

def _strip_trailing_zeros(dec: Decimal) -> str:
    s = f"{dec.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
    return s[:-3] if s.endswith(".00") else s

RATE_REASON = (
    "Due to record fuel costs, a long/demanding route, and tight capacity in this market, "
    "we need a higher rate to service this lane reliably"
)

def build_percentage_reply_flex(base_rate: Optional[Decimal], base_rpm: Optional[Decimal]) -> str:
    percents = [10, 13, 15, 25, 30]
    chunks = []
    for p in percents:
        label = "Broker" if p == 30 else "AI"
        mult = Decimal(1) + (Decimal(p) / Decimal(100))

        new_rate = (base_rate * mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if base_rate is not None else None
        new_rpm  = (base_rpm  * mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if base_rpm  is not None else None

        parts = [f"{p}% {label}"]
        if new_rate is not None and new_rpm is not None:
            parts.append(f"{_fmt_money(new_rate)} /{_fmt_rpm(new_rpm)}/mi")
        elif new_rate is not None:
            parts.append(f"{_fmt_money(new_rate)}")
        elif new_rpm is not None:
            parts.append(f"{_fmt_rpm(new_rpm)}/mi")
        line1 = " ".join(parts)

        shown = new_rate if new_rate is not None else new_rpm
        if shown is not None:
            line2 = f"{_strip_trailing_zeros(shown)} {RATE_REASON}"
            chunks.append(f"{line1}\n{line2}")
        else:
            chunks.append(line1)

    return "\n\n".join(chunks)

# -----------------------------
# PU + offset parsing & scheduling
# -----------------------------
PU_LINE_RE = re.compile(r"(?im)^\s*PU\s*:\s*(.+?)\s*$")
OFFSET_RE = re.compile(r"(?im)^\s*(?:(?P<h>\d{1,3})\s*h)?\s*(?:(?P<m>\d{1,3})\s*m)?\s*$")

# (NEW) "Empty - Preloaded" to'liq ignor qilish
def looks_like_preloaded_empty(text: str) -> bool:
    folded = ascii_fold(text).lower()
    return folded.startswith("empty - preloaded")

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
    Qo'llab-quvvatlanadi:
    - Fri Sep 26 02:30 CDT
    - 5 Sep, 15:40 PDT
    - Sep 5, 15:40 PDT
    - 06:22 AM, 09-26-25, EDT
    - Weekday ixtiyoriy; AM/PM ixtiyoriy; 2 xonali yil ixtiyoriy
    """
    s = pu_str.strip()

    # TZ
    tz_m = re.search(r"\b([A-Za-z]{2,4})\s*$", s)
    tzinfo = _tz_to_zoneinfo(tz_m.group(1)) if tz_m else None

    # dateutil bo'lsa, fuzzy parse
    if du_parser:
        # default yil: hozirgi yil
        default_year = datetime.now(timezone.utc).astimezone().year
        base = datetime(default_year, 1, 1, 0, 0, 0)
        try:
            # 2-digit yearlarni ham qabul qiladi
            dt = du_parser.parse(
                s, fuzzy=True, dayfirst=False, default=base,
                tzinfos=(lambda _name: tzinfo) if tzinfo else None,
            )
            if dt.tzinfo is None and tzinfo:
                dt = dt.replace(tzinfo=tzinfo)
            return dt
        except Exception:
            pass

    # Fallback regexlar
    WEEKDAY_OPT = r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*,?\s+)?"

    # 1) "06:22 AM, 09-26-25, EDT"
    pat_us_ampm = rf"""(?ix)
        ^{WEEKDAY_OPT}
        (?P<h>\d{{1,2}}):(?P<mi>\d{{2}})
        \s*(?P<ampm>AM|PM)\s*,\s*
        (?P<mo>\d{{1,2}})-(?P<d>\d{{1,2}})-(?P<y>\d{{2,4}})
        \s*,\s*(?P<tz>[A-Za-z]{{2,4}})\s*$
    """

    m = re.search(pat_us_ampm, s)
    if m:
        hour = int(m.group("h"))
        minute = int(m.group("mi"))
        ampm = m.group("ampm").upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0
        mon = int(m.group("mo"))
        day = int(m.group("d"))
        year = int(m.group("y"))
        if year < 100:
            year += 2000
        tzinfo = _tz_to_zoneinfo(m.group("tz")) or timezone.utc
        try:
            return datetime(year, mon, day, hour, minute, tzinfo=tzinfo)
        except Exception:
            return None

    # 2) "Fri Sep 26 02:30 CDT" hamda "5 Sep, 15:40 PDT" / "Sep 5, 15:40 PDT"
    pat1 = rf"(?i)^{WEEKDAY_OPT}(?P<d>\d{{1,2}})\s+(?P<mon>[A-Za-z]{{3}}),?\s+(?P<h>\d{{1,2}}):(?P<mi>\d{{2}})\s+(?P<tz>[A-Za-z]{{2,4}})\s*$"
    pat2 = rf"(?i)^{WEEKDAY_OPT}(?P<mon>[A-Za-z]{{3}})\s+(?P<d>\d{{1,2}}),?\s+(?P<h>\d{{1,2}}):(?P<mi>\d{{2}})\s+(?P<tz>[A-Za-z]{{2,4}})\s*$"

    mm = re.search(pat1, s) or re.search(pat2, s)
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
    for line in text.splitlines():
        if "PU:" in line:
            continue
        m = OFFSET_RE.match(line.strip())
        if m and (m.group("h") or m.group("m")):
            return timedelta(hours=int(m.group("h") or 0), minutes=int(m.group("m") or 0))
    return None

# --- Available offsets filtering (NEW) ---
def _available_offsets_for(pu_dt: datetime) -> List[int]:
    """
    PU - offset - 5m > now (+MIN_DELAY_SEC) shartini qanoatlantirgan offsetlarni qaytaradi.
    """
    now_utc = datetime.now(timezone.utc)
    min_fire_time = now_utc + timedelta(seconds=MIN_DELAY_SEC)
    avail: List[int] = []
    for h in OFF_CHOICES:
        fire_at = (pu_dt - timedelta(hours=h) - timedelta(minutes=5)).astimezone(timezone.utc)
        if fire_at > min_fire_time:
            avail.append(h)
    return avail

async def schedule_ai_available_msg(
    when_dt_utc: datetime,
    chat_id: int,
    reply_to_message_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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

    if MIN_DELAY_SEC > 0 and when_dt_utc <= now_utc + timedelta(seconds=MIN_DELAY_SEC):
        when_dt_utc = now_utc + timedelta(seconds=MIN_DELAY_SEC)

    if when_dt_utc <= now_utc + timedelta(seconds=2) and MIN_DELAY_SEC == 0:
        await _job_callback(context)
        return

    job_queue = getattr(context, "job_queue", None) or getattr(context.application, "job_queue", None)
    if job_queue:
        try:
            job_queue.run_once(_job_callback, when=when_dt_utc)
            logger.info("Scheduled AI notice at %s (UTC) for chat=%s msg=%s",
                        when_dt_utc.isoformat(), chat_id, reply_to_message_id)
            return
        except Exception as e:
            logger.exception("Failed to schedule job_queue.run_once: %s", e)

    delay = max(0, int((when_dt_utc - now_utc).total_seconds()))
    async def _sleep_then_send():
        await asyncio.sleep(delay)
        await _job_callback(context)
    asyncio.create_task(_sleep_then_send())
    logger.warning("JobQueue missing; using asyncio fallback with delay=%ss", delay)

# -----------------------------
# Inline keyboard (NEW)
# -----------------------------
OFF_CHOICES = [12, 9, 8, 7, 6, 2, 1]  # soat
CB_PREFIX = "sch"  # callback data prefiksi

def _kb_for(chat_id: int, origin_msg_id: int) -> InlineKeyboardMarkup:
    key = (chat_id, origin_msg_id)
    chosen = PENDING_OFFSETS.get(key, set())
    pu_dt = PENDING_PU.get(key)
    choices = OFF_CHOICES if pu_dt is None else _available_offsets_for(pu_dt)

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for h in choices:
        label = f"{'✅ ' if h in chosen else ''}{h}h"
        row.append(InlineKeyboardButton(label, callback_data=f"{CB_PREFIX}|sel|{origin_msg_id}|{h}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("Submit", callback_data=f"{CB_PREFIX}|sub|{origin_msg_id}"),
        InlineKeyboardButton("Clear",  callback_data=f"{CB_PREFIX}|clr|{origin_msg_id}"),
    ])
    return InlineKeyboardMarkup(rows)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    try:
        parts = q.data.split("|")
        if len(parts) < 3 or parts[0] != CB_PREFIX:
            return
        action = parts[1]
        origin_msg_id = int(parts[2])
        chat_id = q.message.chat_id if q.message else None
        if chat_id is None:
            await q.answer()
            return
        key = (chat_id, origin_msg_id)

        if action == "sel":
            if len(parts) != 4:
                await q.answer()
                return
            h = int(parts[3])
            pu_dt = PENDING_PU.get(key)
            choices = OFF_CHOICES if pu_dt is None else _available_offsets_for(pu_dt)
            if h not in choices:
                await q.answer("That offset is no longer available.")
                await q.edit_message_reply_markup(reply_markup=_kb_for(chat_id, origin_msg_id))
                return
            s = PENDING_OFFSETS.setdefault(key, set())
            if h in s:
                s.remove(h)
            else:
                s.add(h)
            await q.edit_message_reply_markup(reply_markup=_kb_for(chat_id, origin_msg_id))
            await q.answer("Toggled")
            return

        if action == "clr":
            PENDING_OFFSETS.pop(key, None)
            await q.edit_message_reply_markup(reply_markup=_kb_for(chat_id, origin_msg_id))
            await q.answer("Cleared")
            return

        if action == "sub":
            pu_dt = PENDING_PU.get(key)
            sel = sorted(PENDING_OFFSETS.get(key, set()), reverse=True)
            if not pu_dt:
                await q.answer("Time expired or missing.", show_alert=True)
                return
            choices = _available_offsets_for(pu_dt)
            sel = [h for h in sel if h in choices]
            if not sel:
                await q.answer("All selected offsets have passed. Pick new ones.", show_alert=True)
                await q.edit_message_reply_markup(reply_markup=_kb_for(chat_id, origin_msg_id))
                return

            created = []
            for h in sel:
                send_at = pu_dt - timedelta(hours=h) - timedelta(minutes=5)
                send_at_utc = send_at.astimezone(timezone.utc)
                await schedule_ai_available_msg(
                    when_dt_utc=send_at_utc,
                    chat_id=chat_id,
                    reply_to_message_id=origin_msg_id,
                    context=context,
                )
                SCHEDULED[(chat_id, origin_msg_id)] = send_at_utc.isoformat()
                created.append(f"{h}h → {send_at.strftime('%Y-%m-%d %H:%M %Z')}")

            PENDING_OFFSETS.pop(key, None)
            PENDING_PU.pop(key, None)

            await q.edit_message_text("✅ Scheduled (PU − offset − 5m):\n" + "\n".join(created))
            await q.answer("Scheduled")
            return

    except Exception as e:
        logger.exception("Callback failed: %s", e)
        try:
            await update.callback_query.answer("Error", show_alert=True)
        except Exception:
            pass

# -----------------------------
# Core logic
# -----------------------------
TRIP_PROMPT_1 = (
    "Please review all posted trucks—the driver is already covered. If you see a post for a covered truck, remove it.\n\n"
    "It only takes a few seconds—let’s check.\n\n"
    "@dispatchrepublic  @Aziz_157 @d1spa1ch @d1spa1ch_team"
)

def looks_like_trip_post(text: str) -> bool:
    folded = ascii_fold(text).lower()
    return ("trip id" in folded) or ("🗺" in text)

def get_message_text(update: Update) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    return (msg.text or msg.caption or "").strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 TripBot is alive.\n\n"
        "• Reply 'Add 100' / 'Minus 100' (yoki 'Add100') — Rate & $/mi qayta hisoblanadi.\n"
        "• Schedule (PU shartsiz):\n"
        "  Fri Sep 26 02:30 CDT yoki 06:22 AM, 09-26-25, EDT\n"
        "  Offset yozsangiz: `2h` → darhol schedule (PU − offset − 5m)\n"
        "  Offset yozilmasa: inline tugmalar (12h…1h) chiqadi, multi-select + Submit.\n"
        "  'Empty - Preloaded' bloklari ignor qilinadi.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = get_message_text(update)
    if not text:
        return

    # -------- (NEW) Flexible Rate/RPM foiz hisob --------
    try:
        parsed = _parse_flex_rate_rpm(text)
    except Exception as e:
        logger.exception("Flex parser failed: %s", e)
        parsed = None

    if parsed:
        base_rate, base_rpm = parsed
        try:
            reply = build_percentage_reply_flex(base_rate, base_rpm)
            await msg.reply_text(reply)
        except Exception as e:
            logger.exception("Flex rate/rpm reply failed: %s", e)
        return
    # ----------------------------------------------------

    # -------- (A) Schedule (yangilangan) --------
    # "Empty - Preloaded ..." bo'lsa umuman schedulega urinmaymiz
    if looks_like_preloaded_empty(text):
        pass
    else:
        pu_dt: Optional[datetime] = None

        # 1) "PU:" labeled
        pu_line_m = PU_LINE_RE.search(text)
        if pu_line_m:
            pu_dt = parse_pu_datetime(pu_line_m.group(1).strip())
        else:
            # 2) unlabeled: birinchi parse bo‘ladigan qatordan olamiz
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                dt_try = parse_pu_datetime(ln)
                if dt_try:
                    pu_dt = dt_try
                    break

        if pu_dt:
            offs = parse_offset(text)
            if offs:
                send_at = pu_dt - offs - timedelta(minutes=5)
                send_at_utc = send_at.astimezone(timezone.utc)
                try:
                    await msg.reply_text("noted")
                    await schedule_ai_available_msg(
                        when_dt_utc=send_at_utc,
                        chat_id=msg.chat_id,
                        reply_to_message_id=msg.message_id,
                        context=context,
                    )
                    SCHEDULED[(msg.chat_id, msg.message_id)] = send_at_utc.isoformat()
                except Exception as e:
                    logger.exception("Failed to create schedule: %s", e)
                    await msg.reply_text("⚠️ Could not schedule. Check time & offset.")
                return
            else:
                # Offset yo'q — inline keyboard (faqat o‘tmagan offsetlar)
                key = (msg.chat_id, msg.message_id)
                PENDING_PU[key] = pu_dt
                PENDING_OFFSETS[key] = set()

                avail = _available_offsets_for(pu_dt)
                if not avail:
                    await msg.reply_text("⏱ All offsets have already passed for this PU time.")
                    return

                human = pu_dt.strftime("%a, %b %d %I:%M %p %Z")
                try:
                    await msg.reply_text(
                        f"PU: {human}\nSelect offsets (multi-select), then Submit. (Will send at PU − offset − 5m)",
                        reply_markup=_kb_for(msg.chat_id, msg.message_id),
                    )
                except Exception as e:
                    logger.exception("Failed to show inline keyboard: %s", e)
                return
        # Agar pu_dt yo'q bo'lsa — sukut, keyingi bloklarga o'tamiz

    # -------- (B) Trip ID post → prompt --------
    if looks_like_trip_post(text):
        CHAT_LAST_TRIP[msg.chat_id] = text
        try:
            await msg.reply_text(TRIP_PROMPT_1)
        except Exception as e:
            logger.exception("Failed to send trip prompt: %s", e)
        return

    # -------- (C) Add/Minus --------
    folded_cmd = ascii_fold(text).lower()
    m = re.search(r"\b(add|minus)\s*([+-]?\d+(?:\.\d{1,2})?)\b", folded_cmd)
    if m:
        try:
            op = m.group(1)
            delta = Decimal(m.group(2))
            if op == "minus":
                delta = -delta
            if msg.reply_to_message:
                original_trip_text = (msg.reply_to_message.text or msg.reply_to_message.caption or "")
            else:
                original_trip_text = CHAT_LAST_TRIP.get(msg.chat_id) or ""

            base_rate = parse_first_dollar_amount(original_trip_text)
            miles = parse_trip_miles(original_trip_text)

            if base_rate is None or miles is None or miles == 0:
                await msg.reply_text("❗ Rate/Miles topilmadi. '💰 Rate: $123.45' va '🚛 Trip: 431.63mi' bo‘lsin.")
                return

            new_rate = (base_rate + delta).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            new_rpm = (new_rate / miles).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            updated_text = update_rate_and_rpm_in_text(original_trip_text, new_rate, new_rpm)
            if not updated_text:
                # Fallback — Rate satrini yangilab, Per mile qo'shamiz
                updated_text = re.sub(
                    r"(?m)^.*💰[\s\S]*?\$[0-9][\d,]*(?:\.[0-9]{1,4})?.*$",
                    f"💰 𝗥𝗮𝘁𝗲: {format_money(new_rate)}",
                    original_trip_text,
                    count=1,
                )
                if "Per mile" in ascii_fold(updated_text).lower():
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
# Error handler
# -----------------------------
async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception in handler", exc_info=ctx.error)

# -----------------------------
# Entrypoint
# -----------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Export BOT_TOKEN=...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_error_handler(_on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^sch\|"))
    # Muhim: Hamma xabar turlari (text, caption, forward ...) ushlansin
    app.add_handler(MessageHandler(~filters.COMMAND, on_any_message))

    logger.info("Starting TripBot...")

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
