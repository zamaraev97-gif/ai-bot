import os, requests, hmac, hashlib
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, BotCommand
from bot import build_application

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL","")  # например: https://ai-bot-telegram.onrender.com
TRIBUTE_WEBHOOK_SECRET = os.getenv("TRIBUTE_WEBHOOK_SECRET","changeme")

app = FastAPI()
application = None

@app.on_event("startup")
async def _startup():
    global application
    application = build_application()
    await application.initialize()
    await application.start()
    # меню команд (выпадающий список)
    try:
        await application.bot.set_my_commands([
            BotCommand("start","Меню"),
            BotCommand("help","Инструкция"),
            BotCommand("buy","Купить тариф"),
            BotCommand("redeem","Активировать код"),
            BotCommand("voiceon","Включить ответ голосом"),
            BotCommand("voiceoff","Выключить ответ голосом"),
            BotCommand("history","Моя история"),
        ])
    except Exception:
        pass

@app.on_event("shutdown")
async def _shutdown():
    if application:
        await application.stop()

@app.get("/")
async def root():
    return {"ok": True}

# Вебхук Телеграма — сюда Telegram шлёт апдейты
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

# Установка вебхука (вызов извне: curl -X POST https://.../set_webhook)
@app.post("/set_webhook")
async def set_webhook():
    if not PUBLIC_BASE_URL:
        raise HTTPException(400, "PUBLIC_BASE_URL not set")
    url = f"{PUBLIC_BASE_URL.rstrip('/')}/webhook"
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", data={"url": url}, timeout=20)
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text}

# Пример приёмника платежного вебхука (опционально, если будешь использовать Tribute-хук)
@app.post("/payment/tribute")
async def tribute_payment(request: Request):
    body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")
    sent = (payload.get("sign") or "").strip()
    calc = hmac.new(TRIBUTE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not sent or not hmac.compare_digest(sent, calc):
        raise HTTPException(403, "bad signature")

    tg_id = payload.get("tg_id")
    plan  = (payload.get("plan") or "").strip()
    days  = int(payload.get("days") or 30)
    if not tg_id or plan not in ("standard","premium"):
        raise HTTPException(400, "bad payload")

    from bot import set_plan, PLAN_STANDARD, PLAN_PREMIUM
    plan_const = PLAN_STANDARD if plan=="standard" else PLAN_PREMIUM
    set_plan(int(tg_id), plan_const, days)
    return {"ok": True}
