import os, base64
from io import BytesIO
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI, BadRequestError, NotFoundError

load_dotenv()

def _get_cfg():
    tg_token   = os.getenv("TELEGRAM_BOT_TOKEN")
    base_url   = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")
    api_key    = os.getenv("OPENAI_API_KEY")
    text_model = os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile")
    # список кандидатов для vision — бот переберёт по очереди
    env_vision = os.getenv("VISION_MODEL", "").strip()
    vision_candidates = [
        env_vision,                              # что задано в ENV (если есть)
        "llama-3.2-90b-vision-preview",
        "llama-3.2-90b-vision",
        "llama-3.2-11b-vision-preview",
        "llama-3.2-11b-vision",
        "llava-v1.6-34b",                        # совместимая vision-модель
    ]
    # фильтруем пустые/дубликаты
    seen, ordered = set(), []
    for m in vision_candidates:
        if m and m not in seen:
            seen.add(m); ordered.append(m)
    system     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
    return tg_token, base_url, api_key, text_model, ordered, system

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Текст уже работает. Для фото я сам подберу доступную vision‑модель Groq.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_token, base_url, api_key, text_model, _, system = _get_cfg()
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
    tg_token, base_url, api_key, text_model, vision_list, system = _get_cfg()
    client = OpenAI(api_key=api_key, base_url=base_url)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    # 1) берём самый большой вариант фото и скачиваем байты через Bot API
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    try:
        raw: bytearray = await tg_file.download_as_bytearray()
        data_bytes = bytes(raw)
    except Exception:
        buf = BytesIO()
        await tg_file.download_to_memory(out=buf)
        data_bytes = buf.getvalue()

    # 2) готовим data URL
    b64 = base64.b64encode(data_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"
    caption = update.message.caption or "Опиши изображение"

    # 3) пробуем несколько моделей по очереди
    last_err = None
    for model_name in vision_list:
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role":"system","content":system},
                    {"role":"user","content":[
                        {"type":"text","text":caption},
                        {"type":"image_url","image_url":{"url":data_url}}
                    ]}
                ],
                temperature=0.2
            )
            text = resp.choices[0].message.content.strip()
            await update.message.reply_text(f"Модель: {model_name}\n\n{text}")
            return
        except (BadRequestError, NotFoundError) as e:
            # модель снята/нет доступа — пробуем следующую
            last_err = e
            continue
        except Exception as e:
            last_err = e
            break

    # если сюда дошли — ни одна модель не сработала
    msg = f"Не удалось подобрать vision‑модель. Последняя ошибка: {last_err}"
    await update.message.reply_text(msg)

def build_application():
    tg_token, *_ = _get_cfg()
    if not tg_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(tg_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
