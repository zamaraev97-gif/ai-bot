import os, base64, traceback
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI, BadRequestError, APIConnectionError, APIStatusError

load_dotenv()

# Groq (текст/визуал)
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
def _llm_cfg():
    return (
        os.getenv("TELEGRAM_BOT_TOKEN"),
        os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile"),
        os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
    )

# OpenAI Images (генерация) — ИМЕННО api.openai.com
def _img_client():
    key = os.getenv("OPENAI_IMAGES_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_IMAGES_API_KEY is not set")
    return OpenAI(api_key=key, base_url="https://api.openai.com/v1")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Команды:\n"
        "/imagine <описание> [--size 512|768] — сгенерировать изображение\n"
        "Пришли фото с подписью — опишу картинку.\n"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg, base, key, text_model, system = _llm_cfg()
    client = OpenAI(api_key=key, base_url=base)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    resp = client.chat.completions.create(
        model=text_model,
        messages=[{"role":"system","content":system},{"role":"user","content":update.message.text}],
        temperature=0.5,
    )
    await update.message.reply_text(resp.choices[0].message.content.strip())

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg, base, key, text_model, system = _llm_cfg()
    client = OpenAI(api_key=key, base_url=base)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

    # скачиваем bytes через Bot API
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
            {"role":"system","content":system},
            {"role":"user","content":[
                {"type":"text","text":caption},
                {"type":"image_url","image_url":{"url":data_url}}
            ]}
        ],
        temperature=0.2,
    )
    await update.message.reply_text(resp.choices[0].message.content.strip())

def _parse_size(s: str) -> str:
    s = s.lower().strip().replace("x","×")
    if "512" in s: return "512x512"
    if "768" in s: return "768x768"
    return "1024x1024"

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = await update.message.reply_text("Генерирую изображение… ⏳")

        full = (update.message.text or "").strip()
        parts = full.split(" ", 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            await msg.edit_text("Напиши так: /imagine рыжий кот в скафандре — можно добавить --size 512")
            return

        size = "1024x1024"
        if "--size" in prompt:
            try:
                before, after = prompt.split("--size", 1)
                prompt = before.strip()
                size_token = after.strip().split()[0]
                size = _parse_size(size_token)
            except Exception:
                pass

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
        images = _img_client().images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            quality="standard"
        )
        b64 = images.data[0].b64_json
        img_bytes = base64.b64decode(b64)
        await msg.delete()
        await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ({size})")

    except (BadRequestError, APIConnectionError, APIStatusError) as e:
        await update.message.reply_text(f"Ошибка генерации: {e}")
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось сгенерировать: {e}\n{tb}")

def build_application():
    tg, *_ = _llm_cfg()
    if not tg:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(tg).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("imagine", cmd_imagine))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
