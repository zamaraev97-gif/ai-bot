import os, traceback
from io import BytesIO
import replicate
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

load_dotenv()

# --- текстовый LLM через Groq (OpenAI-совместимый) ---
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL   = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
API_KEY    = os.getenv("OPENAI_API_KEY")
TEXT_MODEL = os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile")
SYSTEM     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# --- Replicate для /imagine ---
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

def _parse_prompt_and_ratio(text: str):
    prompt = text
    ratio = "1:1"
    if "--size" in text:
        try:
            before, after = text.split("--size", 1)
            prompt = before.strip()
            token = after.strip().split()[0].lower()
            if token in ("1:1", "16:9", "9:16"):
                ratio = token
        except Exception:
            pass
    return prompt.strip(), ratio

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я умею:\n"
        "• обычный текст — отвечаю как чат\n"
        "• /imagine <описание> [--size 1:1|16:9|9:16] — сгенерировать изображение (Replicate)\n"
    )

# ======== ТЕКСТ ========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        user_text = (update.message.text or "").strip()
        resp = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role":"system","content":SYSTEM},
                      {"role":"user","content":user_text}],
            temperature=0.5
        )
        await update.message.reply_text(resp.choices[0].message.content.strip())
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось ответить: {e}\n{tb}")

# ======== ИЗОБРАЖЕНИЯ ========
async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notice = await update.message.reply_text("Генерирую изображение… ⏳")
    try:
        if not REPLICATE_API_TOKEN:
            await notice.edit_text("Нет REPLICATE_API_TOKEN в переменных окружения Render.")
            return
        text = (update.message.text or "").strip()
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await notice.edit_text("Формат: /imagine кот в очках --size 1:1")
            return

        prompt, ratio = _parse_prompt_and_ratio(parts[1])
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        client = replicate.Client(api_token=REPLICATE_API_TOKEN)
        # официальная модель по имени — без version
        output = client.run(
            "black-forest-labs/flux-schnell",
            input={"prompt": prompt, "aspect_ratio": ratio, "num_outputs": 1}
        )
        if not output:
            await notice.edit_text("Пустой ответ от модели.")
            return

        await notice.delete()
        await update.message.reply_photo(photo=str(output[0]), caption=f"Готово ✅ ({ratio})")

    except Exception as e:
        tb = traceback.format_exc(limit=2)
        try:
            await notice.edit_text(f"Ошибка: {e}\n{tb}")
        except Exception:
            await update.message.reply_text(f"Ошибка: {e}\n{tb}")

def build_application():
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("imagine", cmd_imagine))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
