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

from openai import OpenAI, BadRequestError, PermissionDeniedError

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")

TEXT_PREFS = [
    os.getenv("OPENAI_TEXT_PRIMARY","gpt-5").strip() or "gpt-5",
    "gpt-5-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4o-mini"
]
IMAGE_PREFS = [
    os.getenv("OPENAI_IMAGE_PRIMARY","dall-e-3").strip() or "dall-e-3",
    "gpt-image-1"
]

OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy")
OPENAI_TIMEOUT_S = float(os.getenv("OPENAI_TIMEOUT_S", "60"))

PAYMENT_URL_STANDARD = os.getenv("PAYMENT_URL_STANDARD", "https://example.com/pay-standard")
PAYMENT_URL_PREMIUM  = os.getenv("PAYMENT_URL_PREMIUM",  "https://example.com/pay-premium")
ADMIN_ID             = os.getenv("ADMIN_ID","")

FREE_DAILY_TEXT    = int(os.getenv("FREE_DAILY_LIMIT", "15"))
FREE_DAILY_IMAGE   = int(os.getenv("FREE_DAILY_IMAGE", "3"))
STANDARD_IMG_MONTH = int(os.getenv("STANDARD_IMG_MONTHLY", "20"))

PLAN_FREE, PLAN_STANDARD, PLAN_PREMIUM = "free", "standard", "premium"

def _client():
    return OpenAI(api_key=OPENAI_API_KEY)

# ========= DB (SQLite) =========
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
    row=DBI.one("SELECT auto_mode FROM settings WHERE chat_id=?", (chat_id,))
    if row is None:
        return True  # по умолчанию авто-режим включён
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
"• 🎤 Голосовой чат — отправь voice: я распознаю и отвечу. Для голосового ответа: /voiceon или /voiceoff.\n"
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
        "нарисуй","изобрази","сделай фото","сгенерируй фото","фото","фотографию",
        "картину","арт","иллюстрацию","логотип","аватар","иконку","эмблему",
        "постер","баннер","обложку","визуал","стикер","эмодзи",
        "make an image","generate an image","create an image","draw",
        "image of","picture of","photo of","logo","poster","artwork","illustration","avatar","icon"
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

# ========= Core =========
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

    if get_voice_reply(chat_id):
        try:
            path = await synth_tts(out, chat_id)
            if path.endswith(".ogg"):
                await context.bot.send_voice(chat_id, voice=InputFile(path, filename="reply.ogg"))
            else:
                await context.bot.send_audio(chat_id, audio=InputFile(path, filename="reply.mp3"))
        except Exception:
            pass

async def handle_image(update:Update, context:ContextTypes.DEFAULT_TYPE, text:str):
    chat_id = update.effective_chat.id
    ok, warn, plan = allow_image(chat_id)
    if not ok:
        await update.message.reply_text(warn, reply_markup=KB)
        return

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    client = _client()
    prompt = text.strip()

    errors = []
    img_b = None

    # приоритет: ENV → дефолтный список
    prefs = []
    env_primary = (os.getenv("OPENAI_IMAGE_PRIMARY","") or "").strip()
    if env_primary:
        prefs.append(env_primary)
    # добавляем дефолты (уникально, сохраняя порядок)
    for m in IMAGE_PREFS:
        if m not in prefs:
            prefs.append(m)

    for m in prefs:
        try:
            kwargs = {"model": m, "prompt": prompt, "size": "1024x1024", "response_format": "b64_json"}
            if (m or "").lower() == "dall-e-3":
                kwargs["quality"] = "standard"
            print(f"[IMG] try model={m} prompt={prompt[:80]!r}")
            gen = client.images.generate(**kwargs)
            img_b = None
            try:
                b64 = getattr(gen.data[0], "b64_json", None)
                if b64:
                    img_b = base64.b64decode(b64)
                else:
                    url = getattr(gen.data[0], "url", None)
                    if url:
                        import requests
                        r = requests.get(url, timeout=60)
                        r.raise_for_status()
                        img_b = r.content
                    else:
                        raise ValueError("no b64_json or url in response")
            except Exception as e2:
                raise e2
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
            human += f"\nПричина ({_m}): {_e}\n"
            human += "Проверьте доступ к image‑моделям в OpenAI (billing/verification)."
        await update.message.reply_text(human, reply_markup=KB)
        return

    bio = BytesIO(img_b)
    bio.name = "image.png"
    bio.seek(0)

    if plan == PLAN_FREE:
        inc_img_usage_free(chat_id)
    elif plan == PLAN_STANDARD:
        inc_img_usage_std(chat_id)
    add_history(chat_id, "image", prompt, "[image]")

    await context.bot.send_photo(chat_id, photo=bio, caption="Готово ✅", reply_markup=KB)

async def on_voice(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    ok,warn=allow_text(chat_id)
    if not ok: await update.message.reply_text(warn, reply_markup=KB); return
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)
        file_id = (update.message.voice or update.message.audio).file_id
        tg_file = await context.bot.get_file(file_id)
        buf = BytesIO(); await tg_file.download_to_memory(out=buf); data = buf.getvalue()

        text=None
        for m in ("gpt-4o-mini-transcribe","whisper-1"):
            try:
                tmp=BytesIO(data); tmp.name="voice.ogg"
                res=_client().audio.transcriptions.create(model=m, file=tmp)
                text=(getattr(res,"text",None) or "").strip()
                if text: break
            except Exception:
                continue
        if not text:
            await update.message.reply_text("Не удалось распознать голос.", reply_markup=KB); return

        await handle_chat(update, context, text)
    except Exception as e:
        tb=traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка распознавания: {e}\n{tb}", reply_markup=KB)

async def synth_tts(text:str, chat_id:int)->str:
    """Пытается сгенерить речь в OGG/Opus (для send_voice). Фоллбэк — MP3."""
    t=text.strip()
    if len(t)>800: t=t[:800]
    client=_client()

    # 1) Пытаемся получить opus/ogg (идеально для Telegram voice)
    for m in ("gpt-4o-mini-tts","tts-1"):
        try:
            path=f"/tmp/tts_{chat_id}.ogg"
            with client.audio.speech.with_streaming_response.create(
                model=m, voice=OPENAI_TTS_VOICE, input=t, response_format="opus"
            ) as resp:
                resp.stream_to_file(path)
            return path
        except Exception:
            continue

    # 2) Фоллбэк — MP3
    for m in ("gpt-4o-mini-tts","tts-1"):
        try:
            path=f"/tmp/tts_{chat_id}.mp3"
            with client.audio.speech.with_streaming_response.create(
                model=m, voice=OPENAI_TTS_VOICE, input=t, response_format="mp3"
            ) as resp:
                resp.stream_to_file(path)
            return path
        except Exception:
            continue

    raise RuntimeError("TTS unavailable")

# ========= Commands / Buttons =========
async def cmd_start(update, context): await update.message.reply_text("Выбери действие 👇", reply_markup=KB)
async def cmd_help(update, context):  await update.message.reply_text(HELP_TEXT, reply_markup=KB)

async def cmd_buy(update, context):
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("Купить Стандарт (200₽/мес)", url=PAYMENT_URL_STANDARD)],
        [InlineKeyboardButton("Купить Премиум (500₽/мес)",  url=PAYMENT_URL_PREMIUM )]
    ])
    await update.message.reply_text(
        "После оплаты доступ не открывается автоматически.\n"
        "Мы пришлём код активации. Введи его командой:\n"
        "/redeem КОД",
        reply_markup=kb
    )

def _gen_hist_line(ts, kind, prompt, response):
    dt=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")
    if kind=="image":
        return f"• [{dt}] картинка: {prompt[:60]}…"
    return f"• [{dt}] текст: {prompt[:60]}…"

async def cmd_history(update, context):
    rows=last_history(update.effective_chat.id, 5)
    if not rows: await update.message.reply_text("История пуста.", reply_markup=KB); return
    text="Последние запросы:\n"+"\n".join(_gen_hist_line(*r) for r in rows)
    await update.message.reply_text(text, reply_markup=KB)

async def cmd_voiceon(update, context): set_voice_reply(update.effective_chat.id, True);  await update.message.reply_text("Голосовой ответ: ВКЛ ✅", reply_markup=KB)
async def cmd_voiceoff(update, context): set_voice_reply(update.effective_chat.id, False); await update.message.reply_text("Голосовой ответ: ВЫКЛ ✅", reply_markup=KB)

def _is_admin(update): return ADMIN_ID and str(update.effective_user.id)==str(ADMIN_ID)

async def cmd_grant(update, context):
    if not _is_admin(update): await update.message.reply_text("Недостаточно прав."); return
    args=(update.message.text or "").split()
    if len(args)!=3 or args[1] not in (PLAN_STANDARD,PLAN_PREMIUM):
        await update.message.reply_text("Использование: /grant standard|premium <дней>"); return
    set_plan(update.effective_chat.id, args[1], int(args[2])); await update.message.reply_text("Тариф выдан ✅")

import secrets, string
def _gen_code(n=15):
    alphabet=string.ascii_uppercase+string.digits
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for __ in range(3))[:n+(n-1)//5]

def _create_codes(plan:str, days:int, count:int=1):
    codes=[]
    for _ in range(count):
        c=_gen_code(15)
        DBI.exec("INSERT INTO redeem_codes(code,plan,days,used) VALUES(?,?,?,0)", (c,plan,days))
        codes.append(c)
    return codes

def _redeem(chat_id:int, code:str)->Tuple[bool,str]:
    row=DBI.one("SELECT plan,days,used FROM redeem_codes WHERE code=?", (code,))
    if not row: return False,"Код не найден."
    plan,days,used=row
    if used: return False,"Код уже использован."
    set_plan(chat_id, plan, int(days or 30))
    DBI.exec("UPDATE redeem_codes SET used=1 WHERE code=?", (code,))
    return True,f"Тариф активирован: {plan} на {days} дн."

async def cmd_genredeem(update, context):
    if not _is_admin(update): await update.message.reply_text("Недостаточно прав."); return
    args=(update.message.text or "").split()
    if len(args)<3 or args[1] not in (PLAN_STANDARD,PLAN_PREMIUM):
        await update.message.reply_text("Использование: /genredeem standard|premium <дней> [кол-во]"); return
    days=int(args[2]); count=int(args[3]) if len(args)>=4 else 1
    codes=_create_codes(args[1], days, count)
    await update.message.reply_text("Коды:\n"+"\n".join("- "+c for c in codes))

async def cmd_redeem(update, context):
    args=(update.message.text or "").split()
    if len(args)<2:
        await update.message.reply_text("Использование: /redeem КОД"); return
    ok,msg=_redeem(update.effective_chat.id, args[1].strip().upper())
    await update.message.reply_text(("✅ "+msg) if ok else ("❌ "+msg), reply_markup=KB)

async def cmd_revoke(update, context):
    if not _is_admin(update): await update.message.reply_text("Недостаточно прав."); return
    args=(update.message.text or "").split()
    if len(args)!=2 or not args[1].isdigit():
        await update.message.reply_text("Использование: /revoke <telegram_user_id>"); return
    uid=int(args[1])
    DBI.exec("INSERT INTO plans(chat_id,plan,expires_at) VALUES(?,?,NULL) "
             "ON CONFLICT(chat_id) DO UPDATE SET plan='free', expires_at=NULL", (uid,"free"))
    await update.message.reply_text(f"Снята подписка у {uid} → free")

# Inline callbacks — НИКОГДА НЕ АКТИВИРУЮТ тариф
async def on_callback(update, context):
    q=update.callback_query
    await q.answer()
    data=q.data or ""
    if data.startswith("buy:"):
        await q.edit_message_text(
            "Оплата по кнопке не активирует доступ автоматически.\n"
            "После оплаты введи код: /redeem КОД"
        ); return
    await q.edit_message_text("Ок ✅")

# Текстовые сообщения
async def on_text(update, context):
    text=(update.message.text or "").strip()
    chat_id=update.effective_chat.id
    if text==BTN_CHAT: await update.message.reply_text("Режим: болталка. Пиши вопрос."); return
    if text==BTN_IMG:  await update.message.reply_text("Режим: генерация фото. Опиши идею картинки."); return
    if text==BTN_VOICE: await update.message.reply_text("Отправь voice — распознаю и отвечу. Для голосового ответа: /voiceon или /voiceoff."); return
    if text==BTN_HIST: await cmd_history(update, context); return
    if text==BTN_TARIFF:
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("Купить Стандарт (200₽/мес)", url=PAYMENT_URL_STANDARD)],
            [InlineKeyboardButton("Купить Премиум (500₽/мес)",  url=PAYMENT_URL_PREMIUM )]
        ])
        await update.message.reply_text(PRICING_TEXT, reply_markup=kb); return
    if text==BTN_HELP: await cmd_help(update, context); return

    if get_auto_mode(chat_id):
        intent = detect_intent(text)
        print(f"[INTENT] {intent}: {text[:80]!r}")
        if intent=="image":
            await handle_image(update, context, text); return
        else:
            await handle_chat(update, context, text); return
    await handle_chat(update, context, text)

# Фото от пользователя (анализ)
async def on_photo(update, context):
    chat_id=update.effective_chat.id
    ok,warn=allow_text(chat_id)
    if not ok: await update.message.reply_text(warn, reply_markup=KB); return
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        ph=update.message.photo[-1]
        tg_file = await context.bot.get_file(ph.file_id)
        buf = BytesIO(); await tg_file.download_to_memory(out=buf); img_bytes=buf.getvalue()
        b64 = base64.b64encode(img_bytes).decode("ascii")

        msgs=[{
            "role":"user",
            "content":[
                {"type":"text","text":"Опиши это изображение кратко и по делу."},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
            ]
        }]
        client=_client()
        out=None
        for model in ["gpt-4o","gpt-4.1","gpt-4o-mini"]:
            try:
                r=client.chat.completions.create(model=model, messages=msgs, temperature=0.2)
                out=(r.choices[0].message.content or "").strip()
                if out: break
            except Exception:
                continue
        if not out:
            await update.message.reply_text("Не удалось проанализировать фото.", reply_markup=KB); return
        if get_plan(chat_id)[0]==PLAN_FREE: inc_text_usage(chat_id)
        add_history(chat_id,"text","[photo]",out)
        await update.message.reply_text(out, reply_markup=KB)
    except Exception as e:
        tb=traceback.format_exc(limit=2)
        await update.message.reply_text(f"Ошибка анализа фото: {e}\n{tb}", reply_markup=KB)

def build_application():
    app=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("voiceon", cmd_voiceon))
    app.add_handler(CommandHandler("voiceoff",cmd_voiceoff))
    # админ
    app.add_handler(CommandHandler("grant",   cmd_grant))
    app.add_handler(CommandHandler("genredeem", cmd_genredeem))
    app.add_handler(CommandHandler("redeem",  cmd_redeem))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    # сервисная проверка images
    async def cmd_imgtest(update, context):
        await handle_image(update, context, "a cute orange cat sticker, simple and clean")
    app.add_handler(CommandHandler("imgtest", cmd_imgtest))

    # кнопки/сообщения
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
