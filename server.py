import os, asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from http import HTTPStatus
from telegram import Update

load_dotenv()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
WEBHOOK_PATH    = os.getenv("WEBHOOK_PATH", "/webhook")

app = FastAPI()  # важно: объект называется 'app'

ptb_app = None

@app.on_event("startup")
async def _startup():
    global ptb_app
    # импортируем и запускаем телеграм-бота только на старте
    from bot import build_application
    ptb_app = build_application()
    await ptb_app.initialize()
    await ptb_app.start()

@app.on_event("shutdown")
async def _shutdown():
    global ptb_app
    if ptb_app:
        await ptb_app.stop()
        await ptb_app.shutdown()

@app.get("/")
async def root():
    return {"ok": True}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    global ptb_app
    if not ptb_app:
        return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE)
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.update_queue.put(update)
    return Response(status_code=HTTPStatus.OK)

@app.post("/set_webhook")
async def set_webhook():
    global ptb_app
    if not ptb_app:
        return {"ok": False, "error": "bot not started yet"}
    url = (PUBLIC_BASE_URL or "").rstrip("/") + WEBHOOK_PATH
    ok = await ptb_app.bot.set_webhook(url)
    return {"ok": ok, "url": url}
