import os, base64, urllib.request
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

load_dotenv()

def _get_cfg():
    tg_token   = os.getenv("TELEGRAM_BOT_TOKEN")
    base_url   = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
    api_key    = os.getenv("OPENAI_API_KEY")
    text_model = os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile")
    vision     = os.getenv("VISION_MODEL", "llama-3.2-11b-vision-preview")
    system     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
    return tg_token, base_url, api_key, text_model, vision, system

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Напиши текст или пришли фото с подписью.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_token, base_url, api_key, text_model, vision_model, system = _get_cfg()
    client = OpenAI(api_key=api_key, base_url=base_url)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    user_text = (update.message.text or "").strip()
    resp = client.chat.completions.create(
        model=text_model,
        messages=[{"role":"system","content":system},
                  {"role":"user","content":user_text}],
        temperature=0.5
    )
    await update.message.reply_text(resp.choices[0].message.content.strip())

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_token, base_url, api_key, text_model, vision_model, system = _get_cfg()
    client = OpenAI(api_key=api_key, base_url=base_url)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    # 1) берём самый большой вариант фото
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    tg_file_url = f"https://api.telegram.org/file/bot{tg_token}/{file.file_path}"

    # 2) скачиваем байты и превращаем в data URL (base64)
    try:
        data = urllib.request.urlopen(tg_file_url, timeout=20).read()
    except Exception as e:
        await update.message.reply_text(f"Не смог скачать фото: {e}")
        return

    b64 = base64.b64encode(data).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    caption = update.message.caption or "Опиши изображение"

    # 3) отправляем в Groq как image_url с data: схемой
    try:
        resp = client.chat.completions.create(
            model=vision_model,
            messages=[
                {"role":"system","content":system},
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
    tg_token, *_ = _get_cfg()
    if not tg_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(tg_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
