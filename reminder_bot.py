from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
import logging

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Message handler function
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or update.message.caption or "").upper()
    logger.info(f"Received message: {text}")

    if "NEW LOAD ALERT" in text:
        await update.message.reply_text(
            "Please check all post trucks, the driver was covered! It takes just few seconds, let's do!"
        )
        logger.info("✅ Replied to 'New Load Alert'")

# Main function
def main():
    TOKEN = "7289422688:AAF6s2dq-n9doyGF-4J5fRvkYnbb6o9cNoM"

    # Start application
    application = Application.builder().token(TOKEN).build()

    # Catch all messages, even from other bots
    application.add_handler(MessageHandler(filters.ALL, handle_all_messages))

    # Start polling
    application.run_polling()

if __name__ == "__main__":
    main()
