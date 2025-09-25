# bot.py
import os
import logging
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, Dict, Set

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

# Interaktiv offset tanlash uchun holat
# token -> {"chat_id": int, "reply_to_msg_id": int, "pu_dt": datetime, "selected": set[str], "keyboard_msg_id": int}
PENDING_SCHEDULES: Dict[str, Dict] = {}

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
    if rpm_line_idx is not None:
        if "/mi" in lines[rpm_line_idx]:
            lines[rpm_line_idx] = repl(lines[rpm_line_idx], format_money(new_rpm))
        else:
            lines[rpm_line_idx] = repl(lines[rpm_line_idx], format_money(new_rpm) + "/mi")
    else:
        insert_at = rate_line_idx + 1
        lines.insert(insert_at, f" 💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi")
    return "\n".join(lines)

# =============================
# FLEX: 1 yoki 2 qatorda (faqat sonlar) — $ ixtiyoriy, /mi ixtiyoriy
# =============================
RPM_DECISION_MAX = Decimal("25")
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
            return (v2, v1)
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
    - 5 Sep, 15:40 PDT
    - Sep 5, 15:40 PDT
    - Fri Sep 5 17:50 MDT
    - Fri Sep 26 02:30 CDT
    """
    s = pu_str.strip()

    tz_m = re.search(r"\b([A-Za-z]{2,4})\s*$", s)
    tzinfo = _tz_to_zoneinfo(tz_m.group(1)) if tz_m else None

    if du_parser:
        default_year = datetime.now(timezone.utc).astimezone().year
        base = datetime(default_year, 1, 1, 0, 0, 0)
        try:
            dt = du_parser.parse(
                s, fuzzy=True, dayfirst=True, default=base,
                tzinfos=(lambda _name: tzinfo) if tzinfo else None,
            )
            if dt.tzinfo is None and tzinfo:
                dt = dt.replace(tzinfo=tzinfo)
            return dt
        except Exception:
            pass

    WEEKDAY_OPT = r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*,?\s+)?"
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
# NEW: Inline keyboard helpers (multi-select + Submit) + FUTURE FILTER
# -----------------------------
OFFSETS = ["12h", "9h", "8h", "7h", "6h", "2h", "1h"]

def _gen_token() -> str:
    return os.urandom(6).hex()  # 12 hex chars

def _parse_offset_text(s: str) -> Optional[timedelta]:
    m = OFFSET_RE.match(s.strip())
    if not m:
        return None
    if not (m.group("h") or m.group("m")):
        return None
    return timedelta(hours=int(m.group("h") or 0), minutes=int(m.group("m") or 0))

def _is_future_send_time(pu_dt: datetime, offs: timedelta) -> bool:
    send_at_utc = (pu_dt - offs - timedelta(minutes=5)).astimezone(timezone.utc)
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
    if not avail:
        row_submit = [InlineKeyboardButton("Submit", callback_data=f"ofs2:{token}:submit")]
        return InlineKeyboardMarkup([row_submit])
    row1 = [InlineKeyboardButton(_btn_label(t, selected), callback_data=f"ofs2:{token}:{t}")
            for t in avail[:4]]
    row2 = [InlineKeyboardButton(_btn_label(t, selected), callback_data=f"ofs2:{token}:{t}")
            for t in avail[4:]]
    row3 = [InlineKeyboardButton("Submit", callback_data=f"ofs2:{token}:submit")]
    rows = []
    if row1: rows.append(row1)
    if row2: rows.append(row2)
    rows.append(row3)
    return InlineKeyboardMarkup(rows)

async def _schedule_with_offset(pu_dt: datetime, offs: timedelta, chat_id: int, reply_to: int, ctx: ContextTypes.DEFAULT_TYPE):
    send_at = pu_dt - offs - timedelta(minutes=5)
    send_at_utc = send_at.astimezone(timezone.utc)
    await schedule_ai_available_msg(
        when_dt_utc=send_at_utc,
        chat_id=chat_id,
        reply_to_message_id=reply_to,
        context=ctx,
    )
    SCHEDULED[(chat_id, reply_to)] = send_at_utc.isoformat()

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
        "• Reply 'Add 100' / 'Minus 100' — Rate & $/mi qayta hisoblanadi.\n"
        "• Schedule:\n"
        "  Fri Sep 26 02:30 CDT (caption yoki text)\n"
        "  → 12h/9h/... ni tanlang (bir nechta ham bo'ladi), so'ng Submit.\n"
        "  Bot: har biri uchun PU − offset − 5m vaqtda xabar yuboradi.\n"
        "  Agar keyingi qatorda '6h' yozsangiz, shu offset bilan darhol schedule bo'ladi.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = get_message_text(update)
    if not text:
        return

    # Flexible Rate/RPM foiz hisob
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

    # Schedule: PU sanani aniqlash
    pu_dt: Optional[datetime] = None
    pu_line_m = PU_LINE_RE.search(text)
    if pu_line_m:
        pu_dt = parse_pu_datetime(pu_line_m.group(1).strip())
    else:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in lines:
            dt_try = parse_pu_datetime(ln)
            if dt_try:
                pu_dt = dt_try
                break

    if pu_dt:
        # Agar matnda offset berilgan bo'lsa — darhol schedule
        offs = parse_offset(text)
        if offs:
            # o‘tmishga tushganini tekshiramiz
            if not _is_future_send_time(pu_dt, offs):
                await msg.reply_text("⚠️ This offset is already in the past. Choose another time.")
                return
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

        # Aks holda — multi-select klaviatura yuboramiz (faqat kelajakdagi variantlar)
        token = _gen_token()
        PENDING_SCHEDULES[token] = {
            "chat_id": msg.chat_id,
            "reply_to_msg_id": msg.message_id,
            "pu_dt": pu_dt,
            "selected": set(),
            "keyboard_msg_id": None,
        }
        try:
            sent = await msg.reply_text(
                "Select offsets, then tap Submit:",
                reply_markup=build_offset_keyboard(token, pu_dt)
            )
            PENDING_SCHEDULES[token]["keyboard_msg_id"] = sent.message_id
        except Exception as e:
            logger.exception("Failed to send keyboard: %s", e)
        return

    # Trip ID post → prompt
    if looks_like_trip_post(text):
        CHAT_LAST_TRIP[msg.chat_id] = text
        try:
            await msg.reply_text(TRIP_PROMPT_1)
        except Exception as e:
            logger.exception("Failed to send trip prompt: %s", e)
        return

    # Add/Minus
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
# Callback handler (multi-select + submit) with future checks
# -----------------------------
async def on_offset_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("ofs2:"):
        return
    try:
        _prefix, token, choice = q.data.split(":", 2)
    except Exception:
        await q.answer("Invalid action.")
        return

    cfg = PENDING_SCHEDULES.get(token)
    if not cfg:
        await q.answer("Expired.")
        return

    pu_dt: datetime = cfg["pu_dt"]
    selected: Set[str] = cfg.get("selected", set())

    # SUBMIT
    if choice == "submit":
        # faqat kelajakdagilarni qoldiramiz (runtime tekshiruv)
        valid = []
        for off_txt in selected:
            td = _parse_offset_text(off_txt)
            if td and _is_future_send_time(pu_dt, td):
                valid.append(off_txt)
        if not valid:
            await q.answer("No valid options.")
            try:
                await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, set()))
            except Exception:
                pass
            return

        chat_id = cfg["chat_id"]
        reply_to = cfg["reply_to_msg_id"]
        try:
            for off_txt in sorted(valid, key=lambda s: int(re.match(r"(\d+)", s).group(1)), reverse=True):
                offs = _parse_offset_text(off_txt)
                if offs:
                    await _schedule_with_offset(pu_dt, offs, chat_id, reply_to, context)
            await q.message.reply_text(f"noted ({', '.join(sorted(valid))})")
        except Exception as e:
            logger.exception("Failed to schedule from buttons: %s", e)
            await q.message.reply_text("⚠️ Could not schedule. Check time.")
        PENDING_SCHEDULES.pop(token, None)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.answer("Scheduled.")
        return

    # TOGGLE offset
    if choice in OFFSETS:
        td = _parse_offset_text(choice)
        if not td or not _is_future_send_time(pu_dt, td):
            await q.answer("This option is already in the past.")
            try:
                # mavjud variantlargina qolsin, tanlovni ham tozalaymiz
                avail = set(_available_offsets(pu_dt))
                cfg["selected"] = selected & avail
                await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, cfg["selected"]))
            except Exception:
                pass
            return

        if choice in selected:
            selected.remove(choice)
        else:
            selected.add(choice)
        cfg["selected"] = selected
        try:
            await q.edit_message_reply_markup(reply_markup=build_offset_keyboard(token, pu_dt, selected))
        except Exception as e:
            logger.exception("Failed to refresh keyboard: %s", e)
        await q.answer("Toggled.")
        return

    await q.answer("Unknown option.")

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
    app.add_handler(CallbackQueryHandler(on_offset_button, pattern=r"^ofs2:"))
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


