import os, base64, sqlite3, time, traceback
from io import BytesIO
from typing import List, Tuple
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI, BadRequestError, APIStatusError, PermissionDeniedError

load_dotenv()

# === OpenAI only ===
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY    = os.getenv("OPENAI_API_KEY")          # sk-...
BASE_URL   = "https://api.openai.com/v1"          # фиксируем OpenAI
SYSTEM     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# Приоритеты моделей (можно переопределить через ENV, имена моделей пользователю не показываем)
TEXT_PREFS   = [m.strip() for m in os.getenv(
    "OPENAI_TEXT_PREFS",   "gpt-5,gpt-5-mini,gpt-4o,gpt-4.1-mini"
).split(",") if m.strip()]

VISION_PREFS = [m.strip() for m in os.getenv(
    "OPENAI_VISION_PREFS", "gpt-5,gpt-4o,gpt-4.1,gpt-5-mini"
).split(",") if m.strip()]

# Генерация изображений: сначала DALL·E 3 (не требует верификации), затем gpt-image-1 (если появится доступ)
IMAGE_PRIMARY   = os.getenv("OPENAI_IMAGE_PRIMARY", "dall-e-3")
IMAGE_FALLBACK  = os.getenv("OPENAI_IMAGE_FALLBACK", "gpt-image-1")

# === SQLite: история + выбранный режим на чат ===
DB_PATH = os.getenv("STATE_DB_PATH", "state.db")

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        chat_id INTEGER,
        role TEXT,
        content TEXT,
        ts REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS prefs(
        chat_id INTEGER PRIMARY KEY,
        mode TEXT
    )""")
    return conn

def save_msg(chat_id: int, role: str, content: str):
    conn = _db()
    conn.execute("INSERT INTO messages(chat_id,role,content,ts) VALUES(?,?,?,?)",
                 (chat_id, role, content, time.time()))
    conn.commit(); conn.close()

def load_history(chat_id: int, limit: int = 15) -> List[Tuple[str,str]]:
    conn = _db()
    cur = conn.execute("SELECT role, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?", (chat_id, limit))
    rows = cur.fetchall(); conn.close()
    rows.reverse()
    return rows

def clear_history(chat_id: int):
    conn = _db()
    conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

def get_mode(chat_id: int) -> str:
    conn = _db()
    cur = conn.execute("SELECT mode FROM prefs WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else "chat"

def set_mode(chat_id: int, mode: str):
    conn = _db()
    conn.execute("INSERT INTO prefs(chat_id,mode) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode",
                 (chat_id, mode))
    conn.commit(); conn.close()

# === OpenAI client ===
def _client() -> OpenAI:
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)

# === UI Клавиатура ===
BTN_CHAT = "💬 Болталка"
BTN_IMG  = "🖼️ Генерация фото"
KB = ReplyKeyboardMarkup([[KeyboardButton(BTN_CHAT), KeyboardButton(BTN_IMG)]],
                         resize_keyboard=True, one_time_keyboard=False)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = get_mode(update.effective_chat.id)
    await update.message.reply_text(
        f"Привет! Выбери режим на клавиатуре ниже.\nТекущий режим: {('болталка' if mode=='chat' else 'генерация фото')}",
        reply_markup=KB
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_chat.id)
    await update.message.reply_text("История чата очищена ✅", reply_markup=KB)

# === Хелперы ===
def _parse_size_flag(text: str, default: str = "1024x1024"):
    """
    Для DALL·E 3 допустимы: 1024x1024, 1024x1792, 1792x1024
    Для gpt-image-1 допустимы также квадраты 512/768/1024.
    """
    prompt = text
    size = default
    if "--size" in text:
        try:
            before, after = text.split("--size", 1)
            prompt = before.strip()
            token = after.strip().split()[0].lower()
            if token in ("1024x1024","1024x1792","1792x1024","512x512","768x768"):
                size = token
            elif token in ("1024","768","512"):
                size = f"{token}x{token}"
        except Exception:
            pass
    # DALL·E 3 — только три размера:
    if IMAGE_PRIMARY == "dall-e-3" and size not in ("1024x1024","1024x1792","1792x1024"):
        size = "1024x1024"
    return prompt.strip(), size

# === Обработка текстов c режимами ===
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # Переключение режима по кнопке
    if text == BTN_CHAT:
        set_mode(update.effective_chat.id, "chat")
        await update.message.reply_text("Режим: болталка", reply_markup=KB)
        return
    if text == BTN_IMG:
        set_mode(update.effective_chat.id, "image")
        await update.message.reply_text("Режим: генерация фото\nНапиши описание, можно добавить --size 1024x1792", reply_markup=KB)
        return

    mode = get_mode(update.effective_chat.id)
    if mode == "image":
        await handle_image_generation(update, context, text)
    else:
        await handle_chat(update, context, text)

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    try:
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

        history = load_history(update.effective_chat.id, limit=15)
        messages = [{"role": "system", "content": SYSTEM}]
        for role, content in history:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        for model in TEXT_PREFS:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.5
                )
                out = resp.choices[0].message.content.strip()
                save_msg(update.effective_chat.id, "user", user_text)
                save_msg(update.effective_chat.id, "assistant", out)
                await update.message.reply_text(out, reply_markup=KB)  # без упоминания модели
                return
            except (BadRequestError, APIStatusError, Exception):
                continue

        await update.message.reply_text("Не удалось ответить ни одной моделью.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось ответить: {e}\n{tb}", reply_markup=KB)

async def handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        client = _client()
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        prompt, size = _parse_size_flag(text)

        # 1) Пробуем DALL·E 3 (не требует верификации организации)
        try:
            gen = client.images.generate(model=IMAGE_PRIMARY, prompt=prompt, size=size)
            if hasattr(gen.data[0], "url") and gen.data[0].url:
                await update.message.reply_photo(photo=gen.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                return
            b64 = getattr(gen.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                return
        except PermissionDeniedError:
            # если вдруг и на DALL·E 3 нет прав — пойдём в fallback
            pass
        except BadRequestError:
            # неверные параметры — попробуем fallback
            pass

        # 2) Фоллбэк: gpt-image-1 (нужна верификация, но вдруг уже есть доступ)
        try:
            gen2 = client.images.generate(model=IMAGE_FALLBACK, prompt=prompt, size=size, quality="high")
            b64 = getattr(gen2.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                return
            if hasattr(gen2.data[0], "url") and gen2.data[0].url:
                await update.message.reply_photo(photo=gen2.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                return
        except (PermissionDeniedError, BadRequestError):
            pass

        await update.message.reply_text("Не удалось получить картинку. Попробуй другой запрос или размер.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка генерации: {e}\n{tb}", reply_markup=KB)

# Фото: в любом режиме — анализ изображения (vision)
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = _client()
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
        caption = (update.message.caption or "Опиши изображение").strip()

        for model in VISION_PREFS:
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
                save_msg(update.effective_chat.id, "user", f"[image] {caption}")
                save_msg(update.effective_chat.id, "assistant", out)
                await update.message.reply_text(out, reply_markup=KB)  # без упоминания модели
                return
            except (BadRequestError, APIStatusError, Exception):
                continue

        await update.message.reply_text("Не удалось проанализировать изображение.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка анализа изображения: {e}\n{tb}", reply_markup=KB)

def build_application():
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
