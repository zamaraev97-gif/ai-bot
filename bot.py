import os, base64, traceback
from io import BytesIO
from typing import List
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI, BadRequestError, APIStatusError

load_dotenv()

# === OpenAI only ===
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY    = os.getenv("OPENAI_API_KEY")              # sk-...
BASE_URL   = "https://api.openai.com/v1"              # фиксируем OpenAI эндпоинт

# Можно переопределить через ENV (через запятую), например:
# OPENAI_TEXT_PREFS="gpt-5,gpt-5-mini,gpt-4o,gpt-4.1-mini"
TEXT_PREFS_ENV   = os.getenv("OPENAI_TEXT_PREFS",   "")
VISION_PREFS_ENV = os.getenv("OPENAI_VISION_PREFS", "")

SYSTEM = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# Списки кандидатов по умолчанию (в порядке приоритета)
DEFAULT_TEXT_PREFS  = ["gpt-5", "gpt-5-mini", "gpt-4o", "gpt-4.1-mini"]
DEFAULT_VISION_PREFS= ["gpt-5", "gpt-4o", "gpt-4.1", "gpt-5-mini"]

def _parse_prefs(env_val: str, fallback: List[str]) -> List[str]:
    xs = [x.strip() for x in env_val.split(",") if x.strip()]
    # удаляем дубликаты, сохраняя порядок
    seen, res = set(), []
    for m in (xs or fallback):
        if m and m not in seen:
            seen.add(m); res.append(m)
    return res

TEXT_PREFS   = _parse_prefs(TEXT_PREFS_ENV, DEFAULT_TEXT_PREFS)
VISION_PREFS = _parse_prefs(VISION_PREFS_ENV, DEFAULT_VISION_PREFS)

def _client() -> OpenAI:
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)

def _choose_text_models(user_text: str) -> List[str]:
    # Очень длинный ввод → отдаём приоритет long-context мини
    length = len(user_text or "")
    if length > 8000:
        long_first = ["gpt-4.1-mini (long context)", "gpt-5-mini", "gpt-5", "gpt-4o"]
        # заменим алиас на реальную модель long context
        mapped = [ "gpt-4.1-mini (long context)" if m=="gpt-4.1-mini (long context)" else m for m in TEXT_PREFS ]
        # Вставим long-context в начало, дальше — prefs
        ordered = ["gpt-4.1-mini (long context)"] + [m for m in mapped if m!="gpt-4.1-mini (long context)"]
        # Реальное имя long-context:
        return [m.replace("gpt-4.1-mini (long context)", "gpt-4.1-mini (long context)") for m in ordered]
    # Обычный текст: как задано в prefs
    return TEXT_PREFS

def _choose_vision_models() -> List[str]:
    return VISION_PREFS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я выбираю модель OpenAI автоматически по запросу.\n"
        "• Текст — gpt‑5 / gpt‑5‑mini / gpt‑4o / gpt‑4.1‑mini (с фоллбэками)\n"
        "• Фото — gpt‑5 / gpt‑4o / gpt‑4.1 / gpt‑5‑mini (с фоллбэками)\n"
        "Можешь переопределить порядок через ENV OPENAI_TEXT_PREFS / OPENAI_VISION_PREFS."
    )

# ==== ТЕКСТ ====
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text = (update.message.text or "").strip()
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

        errors = []
        for model in _choose_text_models(user_text):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role":"system","content":SYSTEM},
                              {"role":"user","content":user_text}],
                    temperature=0.5
                )
                out = resp.choices[0].message.content.strip()
                await update.message.reply_text(f"(model: {model})\n\n{out}")
                return
            except BadRequestError as e:
                # invalid model / not enabled / etc → пробуем следующую
                errors.append(f"{model}: {e}")
                continue
            except APIStatusError as e:
                # 429 insufficient_quota или временные — пробуем следующую
                errors.append(f"{model}: {e}")
                continue
            except Exception as e:
                errors.append(f"{model}: {e}")
                continue

        await update.message.reply_text("Не удалось ответить ни одной моделью:\n" + "\n".join(errors[:3]))
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось ответить: {e}\n{tb}")

# ==== ФОТО → ОПИСАНИЕ ====
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        # Скачиваем байты фото из Telegram
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
        caption = (update.message.caption or "Опиши изображение").strip()

        errors = []
        for model in _choose_vision_models():
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role":"system","content":SYSTEM},
                        {"role":"user","content":[
                            {"type":"text","text":caption},
                            {"type":"image_url","image_url":{"url":data_url}}
                        ]}
                    ],
                    temperature=0.2
                )
                out = resp.choices[0].message.content.strip()
                await update.message.reply_text(f"(model: {model})\n\n{out}")
                return
            except BadRequestError as e:
                errors.append(f"{model}: {e}")
                continue
            except APIStatusError as e:
                errors.append(f"{model}: {e}")
                continue
            except Exception as e:
                errors.append(f"{model}: {e}")
                continue

        await update.message.reply_text("Не удалось проанализировать изображение:\n" + "\n".join(errors[:3]))
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка анализа изображения: {e}\n{tb}")

def build_application():
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
