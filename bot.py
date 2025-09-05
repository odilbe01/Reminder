import os
import logging
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# -----------------------------
# Config & Logging
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tripbot")

# -----------------------------
# Utility helpers
# -----------------------------

def ascii_fold(text: str) -> str:
    """Fold Unicode (e.g., 𝗧𝗿𝗶𝗽 𝗜𝗗) to plain ASCII for robust matching."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if ord(ch) < 128)


def parse_first_two_dollar_amounts(text: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Return (rate, per_mile) by grabbing the first two $ amounts found.
    Assumes the message format lists Rate first, then Per mile.
    """
    amounts = re.findall(r"\$\s*([0-9][\d,]*(?:\.[0-9]{1,4})?)", text)
    if len(amounts) < 2:
        return None, None
    try:
        rate = Decimal(amounts[0].replace(",", ""))
        per_mile = Decimal(amounts[1].replace(",", ""))
        return rate, per_mile
    except Exception:  # noqa: BLE001
        return None, None


def parse_trip_miles(text: str) -> Optional[Decimal]:
    """Extract miles from the line starting with the truck emoji or containing 'Trip:'."""
    # Prefer the line with the truck emoji
    m = re.search(r"🚛[\s\S]*?(\d+[\d,]*(?:\.[0-9]{1,3})?)\s*mi\b", text, re.IGNORECASE)
    if not m:
        # Fallback: any 'Trip:' line
        m = re.search(r"(?im)\bTrip\s*:\s*(\d+[\d,]*(?:\.[0-9]{1,3})?)\s*mi\b", ascii_fold(text))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except Exception:  # noqa: BLE001
        return None


def format_money(value: Decimal) -> str:
    # No thousands separators, fixed to 2 decimals, rounding half up
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def update_rate_and_rpm_in_text(original: str, new_rate: Decimal, new_rpm: Decimal) -> Optional[str]:
    """Replace the first two 💰 lines' dollar amounts with the new values.
    - First 💰 line (without '/mi') => Rate
    - Second 💰 line (with '/mi')   => Per mile
    Returns updated text or None if we can't find both lines.
    """
    lines = original.splitlines()

    rate_line_idx = None
    rpm_line_idx = None

    for idx, line in enumerate(lines):
        if "💰" in line and "$" in line:
            if "/mi" in ascii_fold(line).lower():  # per-mile line
                if rpm_line_idx is None:
                    rpm_line_idx = idx
            else:  # likely the rate line
                if rate_line_idx is None:
                    rate_line_idx = idx

    if rate_line_idx is None or rpm_line_idx is None:
        return None

    # Replace first $amount on the target lines
    def replace_first_dollar_amount(line: str, new_amount: str) -> str:
        return re.sub(r"\$\s*[0-9][\d,]*(?:\.[0-9]{1,4})?", new_amount, line, count=1)

    lines[rate_line_idx] = replace_first_dollar_amount(lines[rate_line_idx], format_money(new_rate))
    # Ensure per-mile has '/mi'
    new_rpm_str = format_money(new_rpm) + "/mi"
    # If the line already has '/mi', keep it; otherwise append
    if "/mi" in lines[rpm_line_idx]:
        # Replace only the $amount, keep the rest
        lines[rpm_line_idx] = replace_first_dollar_amount(lines[rpm_line_idx], format_money(new_rpm))
    else:
        lines[rpm_line_idx] = replace_first_dollar_amount(lines[rpm_line_idx], new_rpm_str)

    return "\n".join(lines)


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
        "• Reply 'Add 100' or 'Minus 100' to a Trip message to auto‑recalculate Rate and $/mi.\n"
        "• When someone posts a Trip ID, I auto‑reply with your two guidance messages.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text

    # 1) If a Trip post appears, auto‑reply with two guidance prompts
    if looks_like_trip_post(text):
        try:
            await msg.reply_text(TRIP_PROMPT_1)
            await msg.reply_text(TRIP_PROMPT_2)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to send trip prompts: %s", e)
        return

    # 2) If someone replies 'Add X' or 'Minus X' to a Trip message, recompute
    folded_cmd = ascii_fold(text).strip().lower()
    m = re.search(r"\b(add|minus)\s+(-?\d+(?:\.\d{1,2})?)\b", folded_cmd)
    if m and msg.reply_to_message and msg.reply_to_message.text:
        try:
            op = m.group(1)
            delta = Decimal(m.group(2))
            if op == "minus":
                delta = -delta

            original_trip_text = msg.reply_to_message.text

            base_rate, _old_rpm = parse_first_two_dollar_amounts(original_trip_text)
            miles = parse_trip_miles(original_trip_text)

            if base_rate is None or miles is None or miles == 0:
                await msg.reply_text(
                    "❗ Could not parse Rate/Trip miles from the original message. Please ensure it contains lines like:\n"
                    "'💰 Rate: $123.45' and '🚛 Trip: 431.63mi'",
                )
                return

            new_rate = (base_rate + delta).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            new_rpm = (new_rate / miles).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            updated_text = update_rate_and_rpm_in_text(original_trip_text, new_rate, new_rpm)
            if not updated_text:
                # Fallback: construct a minimal patch by replacing the two 💰 lines altogether
                updated_text = re.sub(
                    r"(?m)^.*💰[\s\S]*?\$[0-9][\d,]*(?:\.[0-9]{1,4})?.*$",
                    f"💰 𝗥𝗮𝘁𝗲: {format_money(new_rate)}",
                    original_trip_text,
                    count=1,
                )
                # Ensure there is a second 💰 line for Per mile
                if "Per mile" in ascii_fold(updated_text):
                    updated_text = re.sub(
                        r"(?m)^.*💰[\s\S]*?\$[0-9][\d,]*(?:\.[0-9]{1,4})?.*/mi.*$",
                        f"💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi",
                        updated_text,
                        count=1,
                    )
                else:
                    # Append a per-mile line near the first 💰 block
                    parts = updated_text.splitlines()
                    insert_at = 0
                    for i, ln in enumerate(parts):
                        if "💰" in ln:
                            insert_at = i + 1
                            break
                    parts.insert(insert_at, f" 💰 𝗣𝗲𝗿 𝗺𝗶𝗹𝗲: {format_money(new_rpm)}/mi")
                    updated_text = "\n".join(parts)

            await msg.reply_text(updated_text)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to update rate: %s", e)
            await msg.reply_text("⚠️ Something went wrong while updating the rate.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set. Export BOT_TOKEN=... from BotFather.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_text))

    logger.info("Starting TripBot...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
