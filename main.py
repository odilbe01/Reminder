import logging
import re
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ContextTypes
from dateutil import parser

# TIME_PATTERN va OFFSET_PATTERN yuqorida aniqlangan bo'lishi kerak
TIME_PATTERN = r"^[A-Z][a-z]{2} [A-Z][a-z]{3} \d{2} \d{2}:\d{2} [A-Z]{2,4}"
OFFSET_PATTERN = r"(\d{1,2})h"

# Scheduler tashqarida aniqlangan deb faraz qilamiz:
# scheduler = AsyncIOScheduler()
# scheduler.start()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg_text = update.message.text or update.message.caption
        if not msg_text:
            return

        print("Received message:", msg_text)

        chat_id = update.message.chat_id
        user_id = update.message.from_user.id

        # Only allow admin/creator users to trigger the bot
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["creator", "administrator"]:
            return

        # 1. Auto-reply to "⚠️ New Load Alert"
        if "⚠️ New Load Alert" in msg_text:
            await update.message.reply_text(
                "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"
            )
            return

        # 2. Parse message like: `Mon Jul 01 14:30 EDT\n2h`
        lines = msg_text.strip().split("\n")
        if len(lines) != 2:
            return

        time_line = lines[0].strip()
        offset_line = lines[1].strip()

        if not re.match(TIME_PATTERN, time_line):
            return

        offset_match = re.match(OFFSET_PATTERN, offset_line)
        if not offset_match:
            return

        dt = parser.parse(time_line)
        offset_hours = int(offset_match.group(1))

        # Convert reminder time
        reminder_time = dt - timedelta(hours=offset_hours, minutes=10)
        if reminder_time < datetime.now(pytz.utc):
            await update.message.reply_text("Skipped")
            return

        await update.message.reply_text("Noted")

        # Schedule the message
        job_id = f"{chat_id}_{reminder_time.timestamp()}"
        scheduler.add_job(
            lambda: context.bot.send_message(chat_id=chat_id, text="PLEASE BE READY, LOAD AI TIME IS CLOSE!"),
            trigger='date',
            run_date=reminder_time,
            id=job_id,
            replace_existing=True
        )

    except Exception as e:
        logging.error(f"Error in handle_message: {e}")
        await update.message.reply_text("An error occurred while processing your request.")
