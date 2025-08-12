import os, base64, traceback
from io import BytesIO
import replicate
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI

load_dotenv()

# --- LLM (текст/визуал) остаётся через Groq ---
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
def _llm_cfg():
    return (
        os.getenv("TELEGRAM_BOT_TOKEN"),
        os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile"),
        os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
    )

# --- вспомогалки ---
def _parse_size(text: str) -> str:
    # конвертируем --size в aspect_ratio для Replicate
    # поддержим квадрат/горизонталь/вертикаль
    s = (text or "").lower()
    if "16:9" in s or " 169" in s: return "16:9"
    if "9:16" in s or " 916" in s: return "9:16"
    return "1:1"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Команды:\n"
        "/imagine <описание> [--size 1:1|16:9|9:16] — сгенерировать изображение (Replicate, FLUX)\n"
        "Пришли фото с подписью — опишу картинку (vision).\n"
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
    # Анализ фото остаётся как раньше (через Groq vision)
    from io import BytesIO
    import base64
    tg, base, key, text_model, system = _llm_cfg()
    client = OpenAI(api_key=key, base_url=base)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

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

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # мгновенный отклик
    notice = await update.message.reply_text("Генерирую изображение… ⏳")
    try:
        full = (update.message.text or "").strip()
        parts = full.split(" ", 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            await notice.edit_text("Напиши: /imagine рыжий кот на луне [--size 1:1|16:9|9:16]")
            return

        # size → aspect_ratio
        aspect_ratio = "1:1"
        if "--size" in prompt:
            try:
                before, after = prompt.split("--size", 1)
                prompt = before.strip()
                aspect_ratio = _parse_size(after)
            except Exception:
                pass

        # Replicate (FLUX.1 schnell). Для офиц. моделей можно вызывать по `{owner}/{name}`.
        # https://replicate.com/black-forest-labs/flux-schnell/api
        token = os.getenv("REPLICATE_API_TOKEN")
        if not token:
            await notice.edit_text("Нет REPLICATE_API_TOKEN в переменных окружения Render.")
            return
        rep_client = replicate.Client(api_token=token)

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
        outputs = rep_client.run(
            "black-forest-labs/flux-schnell",  # офиц. модель, версия не нужна  [oai_citation:1‡replicate.com](https://replicate.com/changelog/2025-08-05-run-all-models-with-the-same-api-endpoint?utm_source=chatgpt.com)
            input={
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "num_outputs": 1
            }
        )
        # outputs — список FileOutput или URL. Возьмём первый и пошлём как фото.
        out = outputs[0]
        if hasattr(out, "read"):  # FileOutput
            data = out.read()
        else:
            # URL — скачивать не будем, просто пришлём как фото по URL
            await notice.delete()
            await update.message.reply_photo(photo=str(out), caption="Готово ✅")
            return

        await notice.delete()
        await update.message.reply_photo(photo=BytesIO(data), caption=f"Готово ✅ ({aspect_ratio})")

    except Exception as e:
        tb = traceback.format_exc(limit=2)
        try:
            await notice.edit_text(f"Ошибка: {e}\n{tb}")
        except Exception:
            await update.message.reply_text(f"Ошибка: {e}\n{tb}")

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
