import logging
import os
import re
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dateutil import parser
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN").strip()
logging.basicConfig(level=logging.INFO)

TIME_PATTERN = r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{2}:\d{2} [A-Z]{3,4}"
OFFSET_PATTERN = r"(\d{1,2})h(?:(\d{1,2})m)?"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    lines = text.splitlines()

    # ⚠️ New Load Alert javobi
    if "⚠️ New Load Alert" in text:
        await update.message.reply_text(
            "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"
        )
        return

    # Reminder scheduling
    if len(lines) >= 2 and re.match(TIME_PATTERN, lines[0]) and re.search(OFFSET_PATTERN, lines[1]):
        time_str = lines[0].strip()
        offset_match = re.search(OFFSET_PATTERN, lines[1].strip())

        if offset_match:
            hours = int(offset_match.group(1))
            minutes = int(offset_match.group(2)) if offset_match.group(2) else 0

            try:
                dt = parser.parse(time_str)
                if dt.tzinfo is None:
                    tzname = time_str.split()[-1]
                    dt = pytz.timezone(tzname).localize(dt)

                reminder_time = dt - timedelta(hours=hours, minutes=minutes + 10)
                delay = (reminder_time - datetime.now(tz=dt.tzinfo)).total_seconds()

                await update.message.reply_text("noted", reply_to_message_id=update.message.message_id)

                if delay > 0:
                    context.job_queue.run_once(
                        send_reminder,
                        when=delay,
                        data={"chat_id": update.effective_chat.id}
                    )
            except Exception as e:
                logging.warning(f"Time parse error: {e}")

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    await context.bot.send_message(
        chat_id=chat_id,
        text="PLEASE BE READY, LOAD AI TIME IS CLOSE!"
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


