import asyncio
import json
import re
from datetime import datetime, timedelta
import pytz
import os
from aiogram import Bot, Dispatcher, types
from aiogram.enums.chat_member_status import ChatMemberStatus
from aiogram.filters import ChatMemberUpdatedFilter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

API_TOKEN = os.getenv("API_TOKEN")
TIMEZONE = pytz.timezone("America/New_York")
GROUP_FILE = "groups.json"

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

DAILY_REMINDER = """🟨 📅 DAILY UPDATER TASK REMINDER 🟨
👇 Please read and follow carefully every day!

🔰 1. NEW DRIVER PROCEDURE
🆕 When a new driver joins the company:
✅ Check in the Relay App if the driver is verified.
✅ Confirm with the Safety Team (G2G message) in the group.
📲 Make sure the driver is taught how to use the Amazon Relay App, follow on-time PU/DEL, and explain the company charges (Timestamp info too).

📋 2. DAILY LOAD FOLLOW-UP
🚚 As soon as dispatch gets a load:
✅ Update the driver’s status on the Planning Board immediately
✅ Confirm there is no reserve on the load
✅ Check if Loadfetcher dispatched PU/DEL times correctly
👥 Mention whether it's a Solo or Team load

🚫 Manually check for restricted roads at all stops:
• 📣 If there’s a restricted road, send it to the Restriction Group
• 👤 Mention Driver Name + VRID
• ☎️ When 15 miles away from restricted area, call the driver and inform about restricted road or no parking zone

📨 When you send a load to the driver:
✅ Get confirmation: “Did you receive the load info?”

💵 If you added/reserved any amount:
✅ Send to Reserve Group
✅ Mention it on the Gross Board

⚠️ If the driver will be charged:
✅ Notify in the Charge Group

🆘 If you created a case on Amazon:
✅ Send Case Number, Driver Name, and Load Number to the Case Group

📧 Check all main company emails every hour for any updates or issues

🕑 Every 2 hours, send #update to the group and:
✅ Track if the driver is on time

🙋‍♂️ If you don’t know the answer to a driver’s question:
🟡 Just say (CHECKING) in the group and follow up later or ask dispatch

📍 3. LIVE DRIVER TRACKING & AMAZON UPDATES
🛰 Track drivers in real time!
🧭 Check Deadhead (DH) miles

⚠️ Update Amazon immediately if driver faces any issue:
• 🛑 Road/facility closure
• 🚧 Traffic delays
• 🚫 Wrong trailer or seal
• ❗️No empty trailer
• 🕒 Load marked “loading” but departure time passed
"""

REPLY_MESSAGE = "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"

def save_chat_id(chat_id):
    if os.path.exists(GROUP_FILE):
        with open(GROUP_FILE, 'r') as f:
            group_ids = json.load(f)
    else:
        group_ids = []

    if chat_id not in group_ids:
        group_ids.append(chat_id)
        with open(GROUP_FILE, 'w') as f:
            json.dump(group_ids, f)

def load_group_ids():
    if os.path.exists(GROUP_FILE):
        with open(GROUP_FILE, 'r') as f:
            return json.load(f)
    return []

async def send_reminder():
    group_ids = load_group_ids()
    for chat_id in group_ids:
        try:
            await bot.send_message(chat_id, DAILY_REMINDER)
        except Exception as e:
            print(f"[X] Failed to send to {chat_id}: {e}")

# Daily scheduler
scheduler.add_job(send_reminder, 'cron', hour=0, minute=0)
scheduler.add_job(send_reminder, 'cron', hour=8, minute=0)
scheduler.add_job(send_reminder, 'cron', hour=16, minute=0)

# When bot is added to a new group
@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=True))
async def new_chat_handler(event: types.ChatMemberUpdated):
    if event.new_chat_member.status == ChatMemberStatus.MEMBER:
        save_chat_id(event.chat.id)
        await bot.send_message(event.chat.id, "✅ Bot added! Daily reminder will now be sent automatically.")

# Message handler: reply to alert and process PU
@dp.message()
async def handle_messages(message: types.Message):
    # Auto reply to ⚠️ New Load Alert
    if message.text and "⚠️ New Load Alert" in message.text:
        await message.reply(REPLY_MESSAGE)

    # PU reminder detection (caption in photo)
    if message.caption and message.photo:
        lines = message.caption.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("PU: ") and re.search(r"\d+[hm]", lines[1]):
            try:
                pu_str = lines[0][4:].strip()
                offset_str = lines[1].strip().lower()

                # Parse offset
                offset_parts = offset_str.replace(" ", "").split("h")
                hours = int(offset_parts[0]) if offset_parts[0] else 0
                minutes = int(offset_parts[1].replace("m", "")) if len(offset_parts) > 1 and offset_parts[1] else 0

                # Parse PU time
                pu_time = datetime.strptime(pu_str, "%a %b %d %H:%M %Z")
                pu_time = TIMEZONE.localize(pu_time)

                # Calculate reminder time
                offset = timedelta(hours=hours, minutes=minutes)
                reminder_time = pu_time - offset - timedelta(minutes=10)

                # Schedule reminder
                scheduler.add_job(
                    bot.send_message,
                    trigger='date',
                    run_date=reminder_time,
                    args=[message.chat.id, "PLEASE BE READY, LOAD AI TIME IS CLOSE!"]
                )
                await message.reply("noted")
            except Exception as e:
                print(f"[!] Failed to parse PU reminder: {e}")

# Launch bot
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
