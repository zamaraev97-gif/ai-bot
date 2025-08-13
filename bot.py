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
API_KEY    = os.getenv("OPENAI_API_KEY")                # sk-...
BASE_URL   = "https://api.openai.com/v1"
SYSTEM     = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# Модели (имена не показываем)
TEXT_PREFS   = [m.strip() for m in os.getenv(
    "OPENAI_TEXT_PREFS",   "gpt-5,gpt-5-mini,gpt-4o,gpt-4.1-mini"
).split(",") if m.strip()]
VISION_PREFS = [m.strip() for m in os.getenv(
    "OPENAI_VISION_PREFS", "gpt-5,gpt-4o,gpt-4.1,gpt-5-mini"
).split(",") if m.strip()]
IMAGE_PRIMARY   = os.getenv("OPENAI_IMAGE_PRIMARY", "dall-e-3")
IMAGE_FALLBACK  = os.getenv("OPENAI_IMAGE_FALLBACK", "gpt-image-1")

# === Тарифы ===
PLAN_FREE       = "free"       # 15 запросов/сутки
PLAN_STANDARD   = "standard"   # 200₽/мес, 20 картинок/мес
PLAN_PREMIUM    = "premium"    # 500₽/мес, без лимитов
FREE_DAILY_LIMIT      = int(os.getenv("FREE_DAILY_LIMIT", "15"))
STANDARD_IMG_MONTHLY  = int(os.getenv("STANDARD_IMG_MONTHLY", "20"))

# === SQLite: история, режим, сессии, usage, планы ===
DB_PATH = os.getenv("STATE_DB_PATH", "state.db")

def _db():
    conn = sqlite3.connect(DB_PATH)
    # messages
    conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        chat_id INTEGER,
        role TEXT,
        content TEXT,
        ts REAL,
        session_id INTEGER
    )""")
    # prefs
    conn.execute("""CREATE TABLE IF NOT EXISTS prefs(
        chat_id INTEGER PRIMARY KEY,
        mode TEXT,
        current_session_id INTEGER
    )""")
    # sessions
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        created_at REAL
    )""")
    # usage (free daily)
    conn.execute("""CREATE TABLE IF NOT EXISTS usage(
        chat_id INTEGER,
        ymd TEXT,
        count INTEGER,
        PRIMARY KEY(chat_id, ymd)
    )""")
    # plans
    conn.execute("""CREATE TABLE IF NOT EXISTS plans(
        chat_id INTEGER PRIMARY KEY,
        plan TEXT,
        expires_at REAL
    )""")
    # img_usage (standard monthly)
    conn.execute("""CREATE TABLE IF NOT EXISTS img_usage(
        chat_id INTEGER,
        ym TEXT,
        count INTEGER,
        PRIMARY KEY(chat_id, ym)
    )""")
    conn.commit()
    return conn

# ——— helpers: время/форматы ———
def _today():
    return datetime.date.today().isoformat()

def _year_month():
    d = datetime.date.today()
    return f"{d.year:04d}-{d.month:02d}"

def _now_title() -> str:
    return "Диалог от " + datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

# ——— usage (free daily) ———
def inc_usage(chat_id: int) -> int:
    conn = _db()
    ymd = _today()
    cur = conn.execute("SELECT count FROM usage WHERE chat_id=? AND ymd=?", (chat_id, ymd))
    row = cur.fetchone()
    if row:
        newc = row[0] + 1
        conn.execute("UPDATE usage SET count=? WHERE chat_id=? AND ymd=?", (newc, chat_id, ymd))
    else:
        newc = 1
        conn.execute("INSERT INTO usage(chat_id,ymd,count) VALUES(?,?,?)", (chat_id, ymd, newc))
    conn.commit(); conn.close()
    return newc

def get_usage(chat_id: int) -> int:
    conn = _db()
    cur = conn.execute("SELECT count FROM usage WHERE chat_id=? AND ymd=?", (chat_id, _today()))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def reset_usage(chat_id: int):
    conn = _db()
    conn.execute("DELETE FROM usage WHERE chat_id=? AND ymd=?", (chat_id, _today()))
    conn.commit(); conn.close()

# ——— img usage (standard monthly) ———
def inc_img_month(chat_id: int) -> int:
    conn = _db()
    ym = _year_month()
    cur = conn.execute("SELECT count FROM img_usage WHERE chat_id=? AND ym=?", (chat_id, ym))
    row = cur.fetchone()
    if row:
        newc = row[0] + 1
        conn.execute("UPDATE img_usage SET count=? WHERE chat_id=? AND ym=?", (newc, chat_id, ym))
    else:
        newc = 1
        conn.execute("INSERT INTO img_usage(chat_id,ym,count) VALUES(?,?,?)", (chat_id, ym, newc))
    conn.commit(); conn.close()
    return newc

def get_img_month(chat_id: int) -> int:
    conn = _db()
    cur = conn.execute("SELECT count FROM img_usage WHERE chat_id=? AND ym=?", (chat_id, _year_month()))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

# ——— sessions/messages ———
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
    conn = _db()
    cur = conn.execute("SELECT current_session_id FROM prefs WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row and row[0]:
        sid = int(row[0])
        cur2 = conn.execute("SELECT id FROM sessions WHERE id=? AND chat_id=?", (sid, chat_id))
        if cur2.fetchone():
            conn.close(); return sid
    title = _now_title(); now = time.time()
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
    cur = conn.execute("SELECT id FROM sessions WHERE id=? AND chat_id=?", (session_id, chat_id))
    if not cur.fetchone():
        conn.close(); return
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
    rows = cur.fetchall(); conn.close()
    return rows

def rename_session(chat_id: int, session_id: int, new_title: str) -> bool:
    conn = _db()
    cur = conn.execute("UPDATE sessions SET title=? WHERE id=? AND chat_id=?", (new_title, session_id, chat_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

def delete_current_session(chat_id: int) -> bool:
    conn = _db()
    cur = conn.execute("SELECT current_session_id FROM prefs WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        conn.close(); return False
    sid = int(row[0])
    conn.execute("DELETE FROM messages WHERE chat_id=? AND session_id=?", (chat_id, sid))
    conn.execute("DELETE FROM sessions WHERE id=? AND chat_id=?", (sid, chat_id))
    title = _now_title(); now = time.time()
    conn.execute("INSERT INTO sessions(chat_id,title,created_at) VALUES(?,?,?)", (chat_id, title, now))
    new_sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE prefs SET current_session_id=? WHERE chat_id=?", (new_sid, chat_id))
    conn.commit(); conn.close()
    return True

def delete_all_user_data(chat_id: int):
    conn = _db()
    conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM usage WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM img_usage WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM prefs WHERE chat_id=?", (chat_id,))
    # планы оставляем (чтобы не терять оплаты); если не нужно — раскомментируй следующую строку:
    # conn.execute("DELETE FROM plans WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

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

# ——— планы ———
def get_plan(chat_id: int) -> Tuple[str, Optional[float]]:
    conn = _db()
    cur = conn.execute("SELECT plan,expires_at FROM plans WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return PLAN_FREE, None
    plan, exp = row[0], row[1]
    if plan in (PLAN_STANDARD, PLAN_PREMIUM) and exp and exp < time.time():
        return PLAN_FREE, None
    return plan, exp

def set_plan(chat_id: int, plan: str, days: int):
    exp = time.time() + days * 86400
    conn = _db()
    conn.execute("INSERT INTO plans(chat_id,plan,expires_at) VALUES(?,?,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET plan=excluded.plan, expires_at=excluded.expires_at",
                 (chat_id, plan, exp))
    conn.execute("DELETE FROM img_usage WHERE chat_id=?", (chat_id,))
    conn.commit(); conn.close()

# ——— OpenAI client ———
def _client() -> OpenAI:
    if not API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)

# ——— Клавиатуры ———
BTN_CHAT = "💬 Болталка"
BTN_IMG  = "🖼️ Генерация фото"
BTN_NEW  = "🆕 Новый диалог"
BTN_LIST = "📜 Мои диалоги"
BTN_DEL  = "🗑 Удалить диалог"
BTN_HELP = "ℹ️ Помощь"
BTN_MENU = "🔙 Меню"
BTN_PRIC = "💳 Тарифы"
BTN_STAT = "👤 Мой статус"
BTN_PRIV = "🔒 Политика"
BTN_WIPE = "🧽 Удалить данные"

KB = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_CHAT), KeyboardButton(BTN_IMG)],
     [KeyboardButton(BTN_NEW),  KeyboardButton(BTN_LIST)],
     [KeyboardButton(BTN_DEL),  KeyboardButton(BTN_HELP)],
     [KeyboardButton(BTN_PRIC), KeyboardButton(BTN_STAT)],
     [KeyboardButton(BTN_PRIV), KeyboardButton(BTN_WIPE)],
     [KeyboardButton(BTN_MENU)]],
    resize_keyboard=True, one_time_keyboard=False
)

HELP_TEXT = (
    "Как пользоваться:\n"
    "• Кнопки внизу помогают быстро переключаться.\n"
    "• Болталка — обычный чат (контекст по текущему диалогу).\n"
    "• Генерация фото — опиши картинку (можно --size 1024x1792).\n"
    "• Мультидиалоги: Новый / Мои диалоги / Удалить / /rename / /export / /reset.\n"
    "• Тарифы: Free (15/сутки), Standard (200₽/мес, 20 img/мес), Premium (500₽/мес, без лимитов).\n"
    "• Приватность — см. “🔒 Политика”. Удалить всё — “🧽 Удалить данные”.\n"
)

PRICING_TEXT = (
    "Тарифы:\n"
    "• Бесплатный — 15 запросов/сутки (текст+картинки суммарно).\n"
    "• Стандарт — 200₽/мес, картинки: 20 в месяц, текст — без ограничений.\n"
    "• Премиум — 500₽/мес, без ограничений.\n\n"
    "Оплату подключим позже (Telegram Payments или внешний провайдер). Пока можно активировать через кнопки ниже."
)

PRIVACY_TEXT = (
    "🔒 Политика конфиденциальности\n\n"
    "• Основа: бот работает на базе моделей ChatGPT от OpenAI (через OpenAI API).\n"
    "• Что отправляем: ваши сообщения, а также вложения (например, изображения для анализа) отправляются в OpenAI для обработки и генерации ответа.\n"
    "• Хранение у нас: история диалогов и настройки сохраняются в нашей базе (SQLite на сервере) для удобства — чтобы помнить контекст и ваши диалоги.\n"
    "• Хранение у OpenAI: обработка и хранение данных регулируются политиками OpenAI. Подробности см. в их документации и политике приватности на сайте OpenAI.\n"
    "• Зачем данные: чтобы отвечать, помнить контекст, улучшать качество сервиса (на нашей стороне — только функционально необходимые данные).\n"
    "• Безопасность: доступ к базе ограничен; ключи находятся в переменных окружения. Пожалуйста, не отправляйте чувствительные данные, если в этом нет необходимости.\n"
    "• Управление данными: вы можете удалить историю текущего диалога (/reset), удалить диалог, либо стереть все свои данные в боте (кнопка “🧽 Удалить данные” или команда /wipe).\n"
    "• Связь: по вопросам приватности и данных — напишите администратору бота.\n"
)

# ——— Команды ———
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid, title = get_current_session(chat_id)
    await update.message.reply_text(
        f"Привет! Текущий диалог: “{title}”.\n\n{HELP_TEXT}",
        reply_markup=KB
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=KB)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню открыто.", reply_markup=KB)

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid, title = get_current_session(chat_id)
    clear_history(chat_id, sid)
    reset_usage(chat_id)
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
    rows = load_history(chat_id, sid, limit=1000)
    if not rows:
        await update.message.reply_text("В этом диалоге пока пусто.", reply_markup=KB)
        return
    lines = [f"TITLE: {title}", f"EXPORTED_AT: {datetime.datetime.now().isoformat()}",
             "-"*40]
    for role, content in rows:
        who = "USER" if role=="user" else "ASSISTANT"
        lines.append(f"{who}: {content}")
    content = "\n".join(lines)
    bio = BytesIO(content.encode("utf-8")); bio.seek(0)
    await update.message.reply_document(document=InputFile(bio, filename=f"dialog_{sid}.txt"),
                                        caption="Экспорт диалога")

async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRIVACY_TEXT, reply_markup=KB)

async def cmd_wipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    delete_all_user_data(chat_id)
    await update.message.reply_text("Все твои данные в боте удалены. Начинаем с чистого листа ✨", reply_markup=KB)

# Псевдо-покупка
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Купить Стандарт (200₽/мес)", callback_data="buy:standard")],
        [InlineKeyboardButton("Купить Премиум (500₽/мес)",  callback_data="buy:premium")]
    ])
    await update.message.reply_text(
        "Выбери тариф. Оплату подключим позже (Telegram Payments или ссылка на оплату).",
        reply_markup=kb
    )

# Админ-команда: /grant standard 30
ADMIN_ID = os.getenv("ADMIN_ID")
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID and str(update.effective_user.id) != str(ADMIN_ID):
        await update.message.reply_text("Недостаточно прав.")
        return
    args = (update.message.text or "").split()
    if len(args) != 3 or args[1] not in (PLAN_STANDARD, PLAN_PREMIUM):
        await update.message.reply_text("Использование: /grant standard|premium <дней>")
        return
    try:
        days = int(args[2])
    except:
        await update.message.reply_text("Дни должны быть числом.")
        return
    set_plan(update.effective_chat.id, args[1], days)
    await update.message.reply_text(f"Выдан тариф {args[1]} на {days} дн. ✅")

# ——— тарифный контроль ———
def _allow_text(chat_id: int) -> Tuple[bool, str]:
    plan, exp = get_plan(chat_id)
    if plan == PLAN_PREMIUM:
        return True, ""
    if plan == PLAN_STANDARD:
        return True, ""
    used = get_usage(chat_id)
    if used >= FREE_DAILY_LIMIT:
        return False, f"Превышен дневной лимит {FREE_DAILY_LIMIT}. Выбери тариф в “{BTN_PRIC}” или попробуй завтра."
    return True, ""

def _allow_image(chat_id: int) -> Tuple[bool, str]:
    plan, exp = get_plan(chat_id)
    if plan == PLAN_PREMIUM:
        return True, ""
    if plan == PLAN_STANDARD:
        used = get_img_month(chat_id)
        if used >= STANDARD_IMG_MONTHLY:
            return False, f"Лимит картинок исчерпан ({STANDARD_IMG_MONTHLY}/мес). Обнови тариф или жди нового месяца."
        return True, ""
    used = get_usage(chat_id)
    if used >= FREE_DAILY_LIMIT:
        return False, f"Бесплатный лимит {FREE_DAILY_LIMIT}/сутки исчерпан. Выбери тариф в “{BTN_PRIC}”."
    return True, ""

# ——— Кнопочные экраны ———
async def show_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sids = list_sessions(chat_id, limit=10)
    if not sids:
        await update.message.reply_text("Диалогов пока нет. Нажми “🆕 Новый диалог”.", reply_markup=KB)
        return
    buttons = [[InlineKeyboardButton(title[:50], callback_data=f"sess:{sid}")] for sid, title, _ in sids]
    await update.message.reply_text("Выбери диалог:", reply_markup=InlineKeyboardMarkup(buttons))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("sess:"):
        try:
            sid = int(data.split(":",1)[1])
            set_current_session(update.effective_chat.id, sid)
            reset_usage(update.effective_chat.id)
            await query.edit_message_text("Диалог переключён ✅", reply_markup=None)
        except Exception:
            await query.edit_message_text("Не удалось переключить диалог.", reply_markup=None)
    elif data == "buy:standard":
        set_plan(update.effective_chat.id, PLAN_STANDARD, 30)
        await query.edit_message_text("Тариф “Стандарт” активирован на 30 дней ✅")
    elif data == "buy:premium":
        set_plan(update.effective_chat.id, PLAN_PREMIUM, 30)
        await query.edit_message_text("Тариф “Премиум” активирован на 30 дней ✅")

# ——— Роутер текста ———
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    ensure_session(chat_id)

    if text == BTN_CHAT:
        set_mode(chat_id, "chat"); await update.message.reply_text("Режим: болталка", reply_markup=KB); return
    if text == BTN_IMG:
        set_mode(chat_id, "image"); await update.message.reply_text("Режим: генерация фото\nНапиши описание, можно --size 1024x1792", reply_markup=KB); return
    if text == BTN_NEW:
        conn = _db()
        conn.execute("INSERT INTO sessions(chat_id,title,created_at) VALUES(?,?,?)",
                     (chat_id, _now_title(), time.time()))
        new_sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO prefs(chat_id,mode,current_session_id) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET current_session_id=excluded.current_session_id",
            (chat_id, get_mode(chat_id), new_sid)
        )
        reset_usage(chat_id)
        conn.commit(); conn.close()
        await update.message.reply_text("Создан новый диалог ✅", reply_markup=KB); return
    if text == BTN_LIST:
        await show_sessions(update, context); return
    if text == BTN_DEL:
        ok = delete_current_session(chat_id)
        if ok:
            reset_usage(chat_id)
            await update.message.reply_text("Диалог удалён. Создан новый пустой ✅", reply_markup=KB)
        else:
            await update.message.reply_text("Не удалось удалить диалог.", reply_markup=KB)
        return
    if text == BTN_HELP:
        await update.message.reply_text(HELP_TEXT, reply_markup=KB); return
    if text == BTN_MENU:
        await update.message.reply_text("Меню открыто.", reply_markup=KB); return
    if text == BTN_PRIC:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить Стандарт (200₽/мес)", callback_data="buy:standard")],
            [InlineKeyboardButton("Купить Премиум (500₽/мес)",  callback_data="buy:premium")]
        ])
        await update.message.reply_text(PRICING_TEXT, reply_markup=kb); return
    if text == BTN_STAT:
        await update.message.reply_text(_status_text(chat_id), reply_markup=KB); return
    if text == BTN_PRIV:
        await update.message.reply_text(PRIVACY_TEXT, reply_markup=KB); return
    if text == BTN_WIPE:
        delete_all_user_data(chat_id)
        await update.message.reply_text("Все твои данные в боте удалены. Начинаем с чистого листа ✨", reply_markup=KB)
        return

    mode = get_mode(chat_id)
    if mode == "image":
        await handle_image_generation(update, context, text)
    else:
        await handle_chat(update, context, text)

def _status_text(chat_id: int) -> str:
    plan, exp = get_plan(chat_id)
    used_today = get_usage(chat_id)
    img_m = get_img_month(chat_id)
    parts = [f"Текущий план: {('Бесплатный' if plan==PLAN_FREE else ('Стандарт' if plan==PLAN_STANDARD else 'Премиум'))}"]
    if plan in (PLAN_STANDARD, PLAN_PREMIUM) and exp:
        dt = datetime.datetime.fromtimestamp(exp).strftime("%d.%m.%Y %H:%M")
        parts.append(f"Активен до: {dt}")
    if plan == PLAN_FREE:
        parts.append(f"Сегодня использовано: {used_today}/{FREE_DAILY_LIMIT}")
    if plan == PLAN_STANDARD:
        parts.append(f"Картинки в этом месяце: {img_m}/{STANDARD_IMG_MONTHLY}")
    return "\n".join(parts)

# ——— Chat (text) ———
async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    ok, warn = _allow_text(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB); return
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
                plan, _ = get_plan(chat_id)
                if plan == PLAN_FREE:
                    inc_usage(chat_id)
                await update.message.reply_text(out, reply_markup=KB)
                return
            except (BadRequestError, APIStatusError, Exception):
                continue
        await update.message.reply_text("Не удалось ответить ни одной моделью.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Не удалось ответить: {e}\n{tb}", reply_markup=KB)

# ——— Image generation ———
async def handle_image_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    ok, warn = _allow_image(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB); return
    try:
        client = _client()
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

        prompt, size = _parse_size_flag(text)

        # 1) DALL·E 3
        try:
            gen = client.images.generate(model=IMAGE_PRIMARY, prompt=prompt, size=size)
            if hasattr(gen.data[0], "url") and gen.data[0].url:
                await update.message.reply_photo(photo=gen.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                _post_image_count(chat_id)
                return
            b64 = getattr(gen.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                _post_image_count(chat_id)
                return
        except PermissionDeniedError:
            pass
        except BadRequestError:
            pass

        # 2) gpt-image-1 (если доступ появится)
        try:
            gen2 = client.images.generate(model=IMAGE_FALLBACK, prompt=prompt, size=size, quality="high")
            b64 = getattr(gen2.data[0], "b64_json", None)
            if b64:
                img_bytes = base64.b64decode(b64)
                await update.message.reply_photo(photo=BytesIO(img_bytes), caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                _post_image_count(chat_id)
                return
            if hasattr(gen2.data[0], "url") and gen2.data[0].url:
                await update.message.reply_photo(photo=gen2.data[0].url, caption=f"Готово ✅ ({size})", reply_markup=KB)
                save_msg(chat_id, sid, "user", f"[imagine] {prompt} ({size})")
                save_msg(chat_id, sid, "assistant", "[image]")
                _post_image_count(chat_id)
                return
        except (PermissionDeniedError, BadRequestError):
            pass

        await update.message.reply_text("Не удалось получить картинку. Попробуй другой запрос или размер.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка генерации: {e}\n{tb}", reply_markup=KB)

def _post_image_count(chat_id: int):
    plan, _ = get_plan(chat_id)
    if plan == PLAN_FREE:
        inc_usage(chat_id)
    elif plan == PLAN_STANDARD:
        inc_img_month(chat_id)

# ——— Vision (анализ фото) ———
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sid, _ = get_current_session(chat_id)
    ok, warn = _allow_text(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB); return
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
                plan, _ = get_plan(chat_id)
                if plan == PLAN_FREE:
                    inc_usage(chat_id)
                await update.message.reply_text(out, reply_markup=KB)
                return
            except (BadRequestError, APIStatusError, Exception):
                continue
        await update.message.reply_text("Не удалось проанализировать изображение.", reply_markup=KB)
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка анализа изображения: {e}\n{tb}", reply_markup=KB)

# ——— UI/команды и роутинг ———
def build_application():
    if not TG_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("menu",    cmd_menu))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("rename",  cmd_rename))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("wipe",    cmd_wipe))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("grant",   cmd_grant))  # админ
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
