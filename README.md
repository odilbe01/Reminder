# TripBot — Auto Replies + Rate Adjust (Add/Minus)

Telegram group bot for Amazon-style Trip posts:
- When a message contains “🗺 Trip ID …”, the bot auto-replies with **two guidance prompts**.
- When someone **replies** with `Add 100` or `Minus 100` to a Trip post, the bot recalculates **Rate** and **$/mi** and sends back the **updated full post**.

## Features
- Robust parsing (supports unicode bold like 𝗧𝗿𝗶𝗽 𝗜𝗗)
- Decimal-safe money math
- Simple deployment (polling). Webhook not required.

## BotFather settings
- **Allow Groups?** → **Allowed (ON)**
- **Group Privacy** → **OFF (Disable privacy)**
- (Optional) Inline Mode / Business Mode / Payments / Domain / Mini App → OFF

## Local quickstart
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt

export BOT_TOKEN="123456:ABC..."      # Windows (Powershell): $env:BOT_TOKEN="123456:ABC..."
python bot.py
```

Add the bot to your group → post a Trip message → reply `Add 100` or `Minus 100`.

### Trip message format (minimum)
- A **Rate** line with a `$` amount, e.g. `💰 Rate: $972.50`
- A **Per mile** line with a `$` amount ending with `/mi`, e.g. `💰 Per mile: $2.25/mi`
- A **Trip** line with miles (or the 🚛 line), e.g. `🚛 Trip: 431.63mi`

> The bot finds the first two `$` amounts as (Rate, $/mi) and the miles from the truck/Trip line.

## Deploy on Render (worker)
1. Push this repo to GitHub.
2. In Render: **New → Background Worker → Connect repo**.
3. Add **Environment variable** `BOT_TOKEN` with your token.
4. Deploy. Procfile already sets `worker: python bot.py`.

## Deploy with Docker
```bash
docker build -t tripbot .
docker run -e BOT_TOKEN="123:ABC..." --name tripbot --restart unless-stopped tripbot
```

## Notes
- The bot uses polling. For webhooks, switch to `app.run_webhook(...)` per python-telegram-bot docs.
- Mentions in auto replies are hardcoded in `TRIP_PROMPT_1` and `TRIP_PROMPT_2`. You can edit them or extend the bot to read from a config.
```
