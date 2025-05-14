import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import pytz

# 🔑 Bot token va guruh ID'larini yozing
API_TOKEN = '7289422688:AAF6s2dq-n9doyGF-4jSfRvkYnbb6o9cNoM'
GROUP_IDS = [-1001234567890, -1009876543210]  # <-- o'z guruhlaringiz ID'larini yozing

# 🕒 Amerika/New_York vaqti bilan ishlash
TIMEZONE = pytz.timezone("America/New_York")

# 📩 Reminder matni
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
• 🕒 Load marked “loading” but departure time passed"""

REPLY_MESSAGE = "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# 🕗 Reminder yuborish
async def send_reminder():
    for group_id in GROUP_IDS:
        try:
            await bot.send_message(group_id, DAILY_REMINDER)
        except Exception as e:
            print(f"[X] Error sending to {group_id}: {e}")

# 🗓 Har kuni 3 marta yuboriladi
scheduler.add_job(send_reminder, trigger='cron', hour=0, minute=0)
scheduler.add_job(send_reminder, trigger='cron', hour=8, minute=0)
scheduler.add_job(send_reminder, trigger='cron', hour=16, minute=0)

# ⚠️ New Load Alert xabari uchun avtomatik reply
@dp.message()
async def alert_reply(message: types.Message):
    if message.text and "⚠️ New Load Alert" in message.text:
        try:
            await message.reply(REPLY_MESSAGE)
        except Exception as e:
            print(f"[!] Reply failed: {e}")

# 🔁 Botni ishga tushurish
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
