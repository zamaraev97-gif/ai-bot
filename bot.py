import os, base64, sqlite3, time, traceback, datetime
from io import BytesIO
from typing import List, Tuple, Optional
from dotenv import load_dotenv
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from openai import OpenAI, BadRequestError, APIStatusError, PermissionDeniedError

load_dotenv()

# === OpenAI only ===
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY    = os.getenv("OPENAI_API_KEY")          # sk-...
BASE_URL   = "https://api.openai.com/v1"          # фиксируем OpenAI
SYSTEM     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# Приоритеты (модель в ответах НЕ показываем)
TEXT_PREFS   = [m.strip() for m in os.getenv(
    "OPENAI_TEXT_PREFS",   "gpt-5,gpt-5-mini,gpt-4o,gpt-4.1-mini"
).split(",") if m.strip()]
VISION_PREFS = [m.strip() for m in os.getenv(
    "OPENAI_VISION_PREFS", "gpt-5,gpt-4o,gpt-4.1,gpt-5-mini"
).split(",") if m.strip()]

# Картинки: по умолчанию DALL·E 3 (не требует верификации), затем gpt-image-1 (если появится доступ)
IMAGE_PRIMARY   = os.getenv("OPENAI_IMAGE_PRIMARY", "dall-e-3")
IMAGE_FALLBACK  = os.getenv("OPENAI_IMAGE_FALLBACK", "gpt-image-1")

# === SQLite: история, режим, СЕССИИ ===
DB_PATH = os.getenv("STATE_DB_PATH", "state.db")

def _db():
    conn = sqlite3.connect(DB_PATH)
    # messages: добавим столбец session_id, если его ещё нет
    conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        chat_id INTEGER,
        role TEXT,
        content TEXT,
        ts REAL
    )""")
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER")
    except sqlite3.OperationalError:
        pass

    # prefs: текущий режим + текущая сессия
    conn.execute("""CREATE TABLE IF NOT EXISTS prefs(
        chat_id INTEGER PRIMARY KEY,
        mode TEXT,
        current_session_id INTEGER
    )""")

    # sessions: список диалогов пользователя
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        created_at REAL
    )""")
    conn.commit()
    return conn

def _now_title() -> str:
    # Заголовок по умолчанию
    return "Диалог от " + datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

def get_mode(chat_id: int) -> str:
    conn = _db()
    cur = conn.execute("SELECT mode FROM prefs WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else "chat"

def set_mode(chat_id: int, mode: str):
    conn = _db()
    conn.execute(
        "INSERT INTO prefs(chat_id,mode,current_session_id) VALUES(?,?,COALESCE((SELECT current_session_id FROM prefs WHERE chat_id=?),NULL)) "
        "ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode",
        (chat_id, mode, chat_id)
    )
    conn.commit(); conn.close()

def ensure_session(chat_id: int) -> int:
    """Гарантирует, что у чата есть текущая сессия. Возвращает session_id."""
    conn = _db()
    cur = conn.execute("SELECT current_session_id FROM prefs WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        sid = int(row[0])
        # проверим, что сессия существует
        cur2 = conn.execute("SELECT id FROM sessions WHERE id=? AND chat_id=?", (sid, chat_id))
        if cur2.fetchone():
            conn.close()
            return sid

    # создаём новую сессию
    title = _now_title()
    now = time.time()
    conn.execute("INSERT INTO sessions(chat_id,title,created_at) VALUES(?,?,?)", (chat_id, title, now))
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO prefs(chat_id,mode,current_session_id) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET current_session_id=excluded.current_session_id",
        (chat_id, "chat", sid)
    )
    conn.commit(); conn.close()
    return sid

def set_current_session(chat_id: int, session_id: int):
    conn = _db()
    # проверим, что сессия принадлежит этому чату
    cur = conn.execute("SELECT id FROM sessions WHERE id=? AND chat_id=?", (session_id, chat_id))
    if not cur.fetchone():
        conn.close()
        return
    conn.execute(
        "INSERT INTO prefs(chat_id,mode,current_session_id) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET current_session_id=excluded.current_session_id",
        (chat_id, get_mode(chat_id), session_id)
    )
    conn.commit(); conn.close()

def list_sessions(chat_id: int, limit: int = 10) -> List[Tuple[int,str,float]]:
    conn = _db()
    cur = conn.execute(
        "SELECT id,title,created_at FROM sessions WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
        (chat_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def rename_session(chat_id: int, session_id: int, new_title: str) -> bool:
    conn = _db()
    cur = conn.execute("UPDATE sessions SET title=? WHERE id=? AND chat_id=?", (new_title, session_id, chat_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

def get_current_session(chat_id: int) -> Tuple[int, str]:
    conn = _db()
    sid = ensure_session(chat_id)
    cur = conn.execute("SELECT title FROM sessions WHERE id=?", (sid,))
    title = cur.fetchone()[0]
    conn.close()
    return sid, title

def save_msg(chat_id: int, session_id: int, role: str, content: str):
    conn = _db()
    conn.execute(
        "INSERT INTO messages(chat_id,role,content,ts,session_id) VALUES(?,?,?,?,?)",
        (chat_id, role, content, time.time(), session_id)
    )
    conn.commit(); conn.close()

def load_history(chat_id: int, session_id: int, limit: int = 20) -> List[Tuple[str,str]]:
    conn = _db()
    cur = conn.execute(
        "SELECT role, content FROM messages WHERE chat_id=? AND session_id=? ORDER BY ts DESC LIMIT ?",
        (chat_id, session_id, limit)
    )
    rows = cur.fetchall(); conn.close()
    rows.reverse()
    return rows

def clear_history(chat_id: int, session_id: Optional[int] = None):
    conn = _db()
    if session_id:
        conn.execute("DELETE FROM messages WHERE chat_id=? AND session_id=?", (chat_id, session_id))
    else:
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

# === OpenAI client ===
def _client() -> OpenAI:
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)

# === Клавиатуры ===
BTN_CHAT = "💬 Болталка"
BTN_IMG  = "🖼️ Генерация фото"
BTN_NEW  = "🆕 Новый диалог"
BTN_LIST = "📜 Мои диалоги"
KB = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_CHAT), KeyboardButton(BTN_IMG)],
     [KeyboardButton(BTN_NEW),  KeyboardButton(BTN_LIST)]],
    resize_keyboard=True, one_time_keyboard=False
)

# === Команды ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid, title = get_current_session(update.effective_chat.id)
    await update.message.reply_text(
        f"Привет! Текущий диалог: “{title}”. Выбери режим ниже или создай новый.\n"
        f"Команды: /rename <новое имя>, /export, /reset",
        reply_markup=KB
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid, title = get_current_session(update.effective_chat.id)
    clear_history(update.effective_chat.id, sid)
    await update.message.reply_text(f"История диалога “{title}” очищена ✅", reply_markup=KB)

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = (update.message.text or "").split(" ", 1)
    if len(args) < 2 or not args[1].strip():
        await update.message.reply_text("Использование: /rename Новое название диалога")
        return
    new_title = args[1].strip()
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    if rename_session(chat_id, sid, new_title):
        await update.message.reply_text(f"Диалог переименован в “{new_title}” ✅", reply_markup=KB)
    else:
        await update.message.reply_text("Не удалось переименовать.", reply_markup=KB)

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid, title = get_current_session(chat_id)
    rows = load_history(chat_id, sid, limit=1000)  # выгрузим много
    if not rows:
        await update.message.reply_text("В этом диалоге пока пусто.", reply_markup=KB)
        return
    lines = [f"TITLE: {title}", f"EXPORTED_AT: {datetime.datetime.now().isoformat()}",
             "-"*40]
    for role, content in rows:
        who = "USER" if role=="user" else "ASSISTANT"
        lines.append(f"{who}: {content}")
    content = "\n".join(lines)
    bio = BytesIO(content.encode("utf-8"))
    bio.seek(0)
    await update.message.reply_document(document=InputFile(bio, filename=f"dialog_{sid}.txt"), caption="Экспорт диалога")

# === Помощники ===
def _parse_size_flag(text: str, default: str = "1024x1024"):
    # DALL·E 3: 1024x1024, 1024x1792, 1792x1024; gpt-image-1: можно ещё 512/768
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
    if IMAGE_PRIMARY == "dall-e-3" and size not in ("1024x1024","1024x1792","1792x1024"):
        size = "1024x1024"
    return prompt.strip(), size

# === Переключатель и обработчики ===
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    ensure_session(chat_id)

    # кнопки
    if text == BTN_CHAT:
        set_mode(chat_id, "chat")
        await update.message.reply_text("Режим: болталка", reply_markup=KB); return
    if text == BTN_IMG:
        set_mode(chat_id, "image")
        await update.message.reply_text("Режим: генерация фото\nНапиши описание, можно --size 1024x1792", reply_markup=KB); return
    if text == BTN_NEW:
        # создать новую сессию и переключиться
        conn = _db()
        conn.execute("INSERT INTO sessions(chat_id,title,created_at) VALUES(?,?,?)",
                     (chat_id, _now_title(), time.time()))
        new_sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO prefs(chat_id,mode,current_session_id) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET current_session_id=excluded.current_session_id",
            (chat_id, get_mode(chat_id), new_sid)
        )
        conn.commit(); conn.close()
        await update.message.reply_text("Создан новый диалог ✅", reply_markup=KB); return
    if text == BTN_LIST:
        await show_sessions(update, context); return

    mode = get_mode(chat_id)
    if mode == "image":
        await handle_image_generation(update, context, text)
    else:
        await handle_chat(update, context, text)

async def show_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sids = list_sessions(chat_id, limit=10)
    if not sids:
        await update.message.reply_text("Диалогов пока нет. Нажми “🆕 Новый диалог”.", reply_markup=KB)
        return
    buttons = []
    for sid, title, created in sids:
        btn = InlineKeyboardButton(title[:50], callback_data=f"sess:{sid}")
        buttons.append([btn])
    await update.message.reply_text("Выбери диалог:", reply_markup=InlineKeyboardMarkup(buttons))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("sess:"):
        try:
            sid = int(data.split(":",1)[1])
            set_current_session(update.effective_chat.id, sid)
            await query.edit_message_text("Диалог переключён ✅", reply_markup=None)
        except Exception:
            await query.edit_message_text("Не удалось переключить диалог.", reply_markup=None)

async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    try:
        client = _client()
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        history = load_history(chat_id, sid, limit=20)
        messages = [{"role": "system", "content": SYSTEM}]
        for role, content in history:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        for model in TEXT_PREFS:  # gpt‑5 в приоритете
            try:
                resp = client.chat.completions.create(model=model, messages=messages, temperature=0.5)
                out = resp.choices[0].message.content.strip()
                save_msg(chat_id, sid, "user", user_text)
                save_msg(chat_id, sid, "assistant", out)
                await update.message.reply_text(out, reply_markup=KB)  # без упоминания модели
                return
            except (BadRequestError, APIStatusError, Exception):
                continue
        await update.message.reply_text("Не удалось ответить ни одной моделью.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось ответить: {e}\n{tb}", reply_markup=KB)

async def handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    try:
        client = _client()
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        prompt, size = _parse_size_flag(text)

        # 1) DALL·E 3 (без верификации)
        try:
            gen = client.images.generate(model=IMAGE_PRIMARY, prompt=prompt, size=size)
            if hasattr(gen.data[0], "url") and gen.data[0].url:
                await update.message.reply_photo(photo=gen.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                return
            b64 = getattr(gen.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                return
        except PermissionDeniedError:
            pass
        except BadRequestError:
            pass

        # 2) Фоллбэк: gpt-image-1 (нужна верификация; если уже включил — сработает)
        try:
            gen2 = client.images.generate(model=IMAGE_FALLBACK, prompt=prompt, size=size, quality="high")
            b64 = getattr(gen2.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                return
            if hasattr(gen2.data[0], "url") and gen2.data[0].url:
                await update.message.reply_photo(photo=gen2.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                return
        except (PermissionDeniedError, BadRequestError):
            pass

        await update.message.reply_text("Не удалось получить картинку. Попробуй другой запрос или размер.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка генерации: {e}\n{tb}", reply_markup=KB)

# Фото: анализ изображений (vision) — сохраняется в текущую сессию
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    try:
        client = _client()
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
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
                save_msg(chat_id, sid, "user", f"[image] {caption}")
                save_msg(chat_id, sid, "assistant", out)
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
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
