import os, time, base64, sqlite3, traceback
from io import BytesIO
from datetime import datetime, timezone
from typing import Optional, Tuple

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, InputFile
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

from openai import OpenAI, BadRequestError, APIStatusError, PermissionDeniedError

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")
TEXT_PREFS = [
    os.getenv("OPENAI_TEXT_PRIMARY","gpt-5").strip(),
    "gpt-5-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4o-mini"
]
IMAGE_PREFS = [
    os.getenv("OPENAI_IMAGE_PRIMARY","dall-e-3").strip(),
    "gpt-image-1"
]
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy")
OPENAI_TIMEOUT_S = float(os.getenv("OPENAI_TIMEOUT_S", "60"))

PAYMENT_URL_STANDARD = os.getenv("PAYMENT_URL_STANDARD", "https://example.com/pay-standard")
PAYMENT_URL_PREMIUM  = os.getenv("PAYMENT_URL_PREMIUM",  "https://example.com/pay-premium")
ADMIN_ID             = os.getenv("ADMIN_ID","")

FREE_DAILY_TEXT   = int(os.getenv("FREE_DAILY_LIMIT", "15"))
FREE_DAILY_IMAGE  = int(os.getenv("FREE_DAILY_IMAGE", "3"))
STANDARD_IMG_MONTH = int(os.getenv("STANDARD_IMG_MONTHLY", "20"))

PLAN_FREE, PLAN_STANDARD, PLAN_PREMIUM = "free", "standard", "premium"

# ========= OPENAI =========
def _client():
    return OpenAI(api_key=OPENAI_API_KEY)

# ========= DB (SQLite — просто и надёжно; на Render подойдёт) =========
DB_PATH = os.getenv("DB_PATH", "bot.db")

class DB:
    def __init__(self, path:str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.ensure()

    def ensure(self):
        c=self.db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS plans(
            chat_id INTEGER PRIMARY KEY, plan TEXT, expires_at REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS usage_daily(
            chat_id INTEGER, ymd TEXT, text_cnt INTEGER DEFAULT 0, img_cnt INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, ymd)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS usage_img_month(
            chat_id INTEGER, ym TEXT, cnt INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, ym)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, ts REAL, kind TEXT, prompt TEXT, response TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER PRIMARY KEY,
            voice_reply INTEGER DEFAULT 0,
            auto_mode  INTEGER DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS redeem_codes(
            code TEXT PRIMARY KEY, plan TEXT, days INTEGER, used INTEGER DEFAULT 0
        )""")
        self.db.commit()

    def exec(self, q, p=()):
        self.db.execute(q,p); self.db.commit()
    def one(self, q, p=()):
        cur=self.db.execute(q,p); return cur.fetchone()
    def all(self, q, p=()):
        cur=self.db.execute(q,p); return cur.fetchall()

DBI=DB(DB_PATH)

# ========= Plans / Usage =========
def _now(): return time.time()
def _ymd(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def _ym():  return datetime.now(timezone.utc).strftime("%Y-%m")

def get_plan(chat_id:int)->Tuple[str, Optional[float]]:
    row=DBI.one("SELECT plan, expires_at FROM plans WHERE chat_id=?", (chat_id,))
    if not row: return PLAN_FREE, None
    plan, exp=row
    if exp and exp<_now(): return PLAN_FREE, None
    return plan or PLAN_FREE, exp

def set_plan(chat_id:int, plan:str, days:int):
    exp = _now()+days*86400 if days and plan!=PLAN_FREE else None
    DBI.exec("INSERT INTO plans(chat_id,plan,expires_at) VALUES(?,?,?) "
             "ON CONFLICT(chat_id) DO UPDATE SET plan=excluded.plan, expires_at=excluded.expires_at",
             (chat_id, plan, exp))

def inc_text_usage(chat_id:int):
    DBI.exec("INSERT INTO usage_daily(chat_id,ymd,text_cnt,img_cnt) VALUES(?,?,1,0) "
             "ON CONFLICT(chat_id,ymd) DO UPDATE SET text_cnt=text_cnt+1", (chat_id,_ymd()))

def inc_img_usage_free(chat_id:int):
    DBI.exec("INSERT INTO usage_daily(chat_id,ymd,text_cnt,img_cnt) VALUES(?,?,0,1) "
             "ON CONFLICT(chat_id,ymd) DO UPDATE SET img_cnt=img_cnt+1", (chat_id,_ymd()))

def inc_img_usage_std(chat_id:int):
    DBI.exec("INSERT INTO usage_img_month(chat_id,ym,cnt) VALUES(?,?,1) "
             "ON CONFLICT(chat_id,ym) DO UPDATE SET cnt=cnt+1", (chat_id,_ym()))

def get_text_usage_today(chat_id:int)->int:
    row=DBI.one("SELECT text_cnt FROM usage_daily WHERE chat_id=? AND ymd=?", (chat_id,_ymd()))
    return int(row[0]) if row else 0

def get_img_usage_today_free(chat_id:int)->int:
    row=DBI.one("SELECT img_cnt FROM usage_daily WHERE chat_id=? AND ymd=?", (chat_id,_ymd()))
    return int(row[0]) if row else 0

def get_img_usage_month_std(chat_id:int)->int:
    row=DBI.one("SELECT cnt FROM usage_img_month WHERE chat_id=? AND ym=?", (chat_id,_ym()))
    return int(row[0]) if row else 0

def get_voice_reply(chat_id:int)->bool:
    row=DBI.one("SELECT voice_reply FROM settings WHERE chat_id=?", (chat_id,))
    return bool(row and row[0])

def set_voice_reply(chat_id:int, val:bool):
    DBI.exec("INSERT INTO settings(chat_id,voice_reply) VALUES(?,?) "
             "ON CONFLICT(chat_id) DO UPDATE SET voice_reply=excluded.voice_reply", (chat_id, 1 if val else 0))

def get_auto_mode(chat_id:int)->bool:
    row = DBI.one("SELECT auto_mode FROM settings WHERE chat_id=?", (chat_id,))
    # если настроек нет — считаем, что авто-режим ВКЛ по умолчанию
    if row is None:
        return True
    try:
        return bool(int(row[0]) != 0)
    except Exception:
        return True

def set_auto_mode(chat_id:int, val:bool):
    DBI.exec("INSERT INTO settings(chat_id,auto_mode) VALUES(?,?) "
             "ON CONFLICT(chat_id) DO UPDATE SET auto_mode=excluded.auto_mode", (chat_id, 1 if val else 0))

def add_history(chat_id:int, kind:str, prompt:str, response:str):
    DBI.exec("INSERT INTO history(chat_id,ts,kind,prompt,response) VALUES(?,?,?,?,?)",
             (chat_id,_now(),kind,prompt,response))

def last_history(chat_id:int, n:int=5):
    return DBI.all("SELECT ts,kind,prompt,response FROM history WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                   (chat_id, n))

# ========= UI =========
BTN_CHAT="💬 Болталка"
BTN_IMG="🎨 Генерация фото"
BTN_VOICE="🎤 Голосовой чат"
BTN_HIST="📜 Моя история"
BTN_TARIFF="💳 Тарифы"
BTN_HELP="ℹ Инструкция"

KB = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_CHAT), KeyboardButton(BTN_IMG)],
     [KeyboardButton(BTN_VOICE), KeyboardButton(BTN_HIST)],
     [KeyboardButton(BTN_TARIFF), KeyboardButton(BTN_HELP)]],
    resize_keyboard=True
)

HELP_TEXT=(
"👋 Привет! Я помогу поболтать, сгенерировать картинку и распознать голос.\n\n"
"Как пользоваться:\n"
"• 💬 Болталка — просто пиши вопросы.\n"
"• 🎨 Генерация фото — опиши идею картинки (без доп.параметров).\n"
"• 🎤 Голосовой чат — отправь voice: я распознаю и отвечу. Хочешь — включу ответ голосом.\n"
"• 📜 Моя история — последние 5 запросов.\n\n"
"Тарифы:\n"
"🆓 Бесплатно — 15 текстовых / 3 картинки в день.\n"
"💼 Стандарт — 200₽/мес (20 картинок/мес, текст без лимита).\n"
"👑 Премиум — 500₽/мес (всё без ограничений).\n"
"Платные планы активируются только кодом: /redeem КОД\n"
)

PRICING_TEXT=(
"💳 Тарифы\n\n"
"🆓 Бесплатный — 15 текстовых / 3 картинки в день.\n"
"💼 Стандарт — 200₽/мес, 20 картинок/мес.\n"
"👑 Премиум — 500₽/мес, без ограничений.\n\n"
"После оплаты получишь код и активируешь: /redeem КОД"
)

# ========= Intent =========
def detect_intent(text:str)->str:
    t=(text or "").lower()
    markers=[
        "сгенерируй","создай картинку","создай изображение","сделай картинку","сделай изображение",
        "нарисуй","изобрази","постер","логотип","обложку","арт","иллюстрацию","баннер","визуал",
        "make an image","generate an image","create an image","draw","poster","logo","artwork","illustration",
    ]
    return "image" if any(k in t for k in markers) else "chat"

# ========= Access checks =========
def allow_text(chat_id:int)->Tuple[bool,str]:
    plan,_=get_plan(chat_id)
    if plan==PLAN_FREE:
        used=get_text_usage_today(chat_id)
        if used>=FREE_DAILY_TEXT:
            return False,"❌ Лимит бесплатных текстовых запросов на сегодня исчерпан. Оформите тариф в меню «💳 Тарифы»."
    return True,""

def allow_image(chat_id:int)->Tuple[bool,str,str]:
    plan,_=get_plan(chat_id)
    if plan==PLAN_FREE:
        used=get_img_usage_today_free(chat_id)
        if used>=FREE_DAILY_IMAGE:
            return False,"❌ Лимит бесплатных картинок на сегодня исчерпан. Оформите тариф в меню «💳 Тарифы».",PLAN_FREE
        return True,"",PLAN_FREE
    if plan==PLAN_STANDARD:
        used=get_img_usage_month_std(chat_id)
        if used>=STANDARD_IMG_MONTH:
            return False,"❌ Лимит картинок по «Стандарт» исчерпан за месяц. Обновите тариф.",PLAN_STANDARD
        return True,"",PLAN_STANDARD
    return True,"",PLAN_PREMIUM

# ========= Core: text / image / voice =========
async def handle_chat(update:Update, context:ContextTypes.DEFAULT_TYPE, text:str):
    chat_id=update.effective_chat.id
    ok,warn=allow_text(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB); return
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    msgs=[{"role":"system","content":"Ты дружелюбный, краткий и полезный помощник."},
          {"role":"user","content":text}]
    client=_client()
    out=None
    for model in TEXT_PREFS:
        try:
            r=client.chat.completions.create(model=model, messages=msgs, temperature=0.6, timeout=OPENAI_TIMEOUT_S)
            out=(r.choices[0].message.content or "").strip()
            if out: break
        except Exception:
            continue
    if not out:
        await update.message.reply_text("Не удалось ответить. Попробуйте ещё раз.", reply_markup=KB); return

    if get_plan(chat_id)[0]==PLAN_FREE: inc_text_usage(chat_id)
    add_history(chat_id,"text",text,out)
    await update.message.reply_text(out, reply_markup=KB)

    # при включённом голосовом ответе — озвучим
    if get_voice_reply(chat_id):
        try:
            path = await synth_tts(out, chat_id)
            await context.bot.send_audio(chat_id, audio=InputFile(path, filename="reply.mp3"))
        except Exception:
            pass


async def handle_image(update:Update, context:ContextTypes.DEFAULT_TYPE, text:str):
    chat_id = update.effective_chat.id
    ok, warn, plan = allow_image(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB); return

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    client = _client()
    prompt = text.strip()

    errors = []
    img_b = None

    # Явные параметры: 1024x1024, стандартное качество.
    # Если в окружении есть OPENAI_IMAGE_PRIMARY — используем его в приоритете.
    prefs = []
    try:
        from os import getenv
        p = (getenv("OPENAI_IMAGE_PRIMARY","") or "").strip()
        if p: prefs.append(p)
    except Exception:
        pass
    from itertools import chain
    prefs = list(dict.fromkeys(chain(prefs, IMAGE_PREFS)))  # уникальный порядок

    for m in prefs:
        try:
            kwargs = {"model": m, "prompt": prompt, "size": "1024x1024"}
            if (m or "").lower() == "dall-e-3":
                kwargs["quality"] = "standard"
            print(f"[IMG] try model={m} prompt={prompt[:80]!r}")
            gen = client.images.generate(**kwargs)
            b64 = gen.data[0].b64_json
            img_b = base64.b64decode(b64)
            print(f"[IMG] success model={m}")
            break
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"[IMG-ERR] model={m} -> {msg}")
            errors.append((m, msg))
            continue

    if not img_b:
        human = "Не удалось сгенерировать изображение."
        if errors:
            _m, _e = errors[0]
            _e = str(_e)
            if len(_e) > 280:
                _e = _e[:280] + "…"
            human += f"
Причина ({_m}): {_e}
"
            human += "Проверьте доступ к image‑моделям в OpenAI (billing/verification)."
        await update.message.reply_text(human, reply_markup=KB)
        return app


async def cmd_imgtest(update, context):
    # безопасный тестовый промпт
    await handle_image(update, context, "a cute orange cat sticker, simple and clean")

