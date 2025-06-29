import os
import re
import logging
from datetime import datetime, timedelta
import pytz
from dateutil import parser
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO)

app = ApplicationBuilder().token(BOT_TOKEN).build()
scheduler = BackgroundScheduler()
scheduler.start()

TIME_PATTERN = r"^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{2}:\d{2} [A-Z]{3,4}"
OFFSET_PATTERN = r"(\d{1,2})h"

scheduled_jobs = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    msg = update.message.text.strip()
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id

    if not (await context.bot.get_chat_member(chat_id, user_id)).status in ["creator", "administrator"]:
        return

    if "⚠️ New Load Alert" in msg:
        await update.message.reply_text(
            "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"
        )
        return

    lines = msg.split("\n")
    if len(lines) != 2:
        return

    time_line, offset_line = lines[0].strip(), lines[1].strip()
    if not re.match(TIME_PATTERN, time_line):
        return

    try:
        dt = parser.parse(time_line)
        offset_match = re.match(OFFSET_PATTERN, offset_line)
        if not offset_match:
            return
        offset_hours = int(offset_match.group(1))
        reminder_time = dt - timedelta(hours=offset_hours, minutes=10)

        if reminder_time < datetime.now(pytz.utc):
            await update.message.reply_text("Skipped")
            return

        await update.message.reply_text("Noted")

        job_id = f"{chat_id}_{reminder_time.timestamp()}"

        scheduler.add_job(
            lambda: context.bot.send_message(chat_id=chat_id, text="PLEASE BE READY, LOAD AI TIME IS CLOSE!"),
            trigger='date',
            run_date=reminder_time,
            id=job_id,
            replace_existing=True
        )
    except Exception as e:
        logging.error(f"Error parsing message: {e}")

app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

if __name__ == '__main__':
    app.run_polling()
