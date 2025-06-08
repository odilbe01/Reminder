import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# Loglarni sozlash
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ⚠️ New Load Alert'ga reply beruvchi handler
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or update.message.caption or "").upper()
    logger.info(f"Received message: {text}")

    if "NEW LOAD ALERT" in text:
        await update.message.reply_text(
            "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"
        )
        logger.info("✅ Replied to 'New Load Alert'")

# Asosiy ishga tushirish funksiyasi
def main():
    # Bot tokeni shu yerga yozing
    TOKEN = "7289422688:AAF6s2dq-n9doyGF-4jSfRvkYnbb6o9cNoM"

    # Applicationni allowed_updates bilan ishga tushirish
    application = Application.builder().token(TOKEN).allowed_updates(["message"]).build()

    # filters.ALL orqali barcha xabarlarni qabul qilamiz
    application.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    # Botni ishga tushirish
    application.run_polling()

if __name__ == "__main__":
    main()
