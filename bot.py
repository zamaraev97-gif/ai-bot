import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь мне текст, и я сгенерирую изображение через Replicate.")

# Обработчик текстовых сообщений
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text
    try:
        image_url = generate_image(prompt)
        if image_url:
            await update.message.reply_photo(photo=image_url)
        else:
            await update.message.reply_text("Не удалось сгенерировать изображение.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# Функция генерации через Replicate
def generate_image(prompt):
    url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "version": "a16ba0b1f30ddf6ffb7e751063ba1fdfc8646e26e99b3a73a5800d34fecd4c3c", # stable-diffusion
        "input": {
            "prompt": prompt
        }
    }
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    prediction = response.json()

    # Ждём, пока модель завершит работу
    status = prediction["status"]
    while status != "succeeded" and status != "failed":
        r = requests.get(f"https://api.replicate.com/v1/predictions/{prediction['id']}", headers=headers)
        r.raise_for_status()
        prediction = r.json()
        status = prediction["status"]

    if status == "succeeded":
        return prediction["output"][0]
    return None

def build_application():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
