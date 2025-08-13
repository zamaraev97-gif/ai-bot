import os, base64
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

load_dotenv()

# === Конфиг под OpenAI ===
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL      = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")  # OpenAI
API_KEY       = os.getenv("OPENAI_API_KEY")  # ключ вида sk-...
TEXT_MODEL    = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
VISION_MODEL  = os.getenv("VISION_MODEL", "gpt-4o-mini")
SYSTEM        = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

def _client():
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пиши текст — отвечу. Пришли фото с подписью — опишу картинку. (OpenAI)")

# --- Текст → ответ (OpenAI) ---
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        user_text = (update.message.text or "").strip()
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role":"system","content":SYSTEM},
                {"role":"user","content":user_text}
            ],
            temperature=0.5
        )
        await update.message.reply_text(resp.choices[0].message.content.strip())
    except Exception as e:
        await update.message.reply_text(f"Не удалось ответить: {e}")

# --- Фото → описание (OpenAI multimodal) ---
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        # забираем байты фото через Bot API (без внешних URL)
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        try:
            raw: bytearray = await tg_file.download_as_bytearray()
            data_bytes = bytes(raw)
        except Exception:
            buf = BytesIO()
            await tg_file.download_to_memory(out=buf)
            data_bytes = buf.getvalue()

        b64 = base64.b64encode(data_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"
        caption = update.message.caption or "Опиши изображение"

        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {"role":"system","content":SYSTEM},
                {"role":"user","content":[
                    {"type":"text","text":caption},
                    {"type":"image_url","image_url":{"url":data_url}}
                ]}
            ],
            temperature=0.2
        )
        await update.message.reply_text(resp.choices[0].message.content.strip())
    except Exception as e:
        await update.message.reply_text(f"Ошибка анализа изображения: {e}")

def build_application():
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
