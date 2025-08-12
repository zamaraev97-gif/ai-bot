import os, base64, traceback, json
from io import BytesIO
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from openai import OpenAI, BadRequestError, APIConnectionError, APIStatusError, NotFoundError

load_dotenv()

# --- LLM через Groq (совместимый OpenAI endpoint) ---
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

def _llm_cfg():
    return (
        os.getenv("TELEGRAM_BOT_TOKEN"),
        os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile"),
        os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
    )

# --- Images: приоритет Stability → затем OpenAI ---
STABILITY_URL = "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image"

def _img_size(sz: str) -> tuple[int,int]:
    sz = (sz or "").lower()
    if "512" in sz:  return 512, 512
    if "768" in sz:  return 768, 768
    return 1024, 1024

def _gen_via_stability(prompt: str, size: str|None) -> bytes:
    api_key = os.getenv("STABILITY_API_KEY")
    if not api_key:
        raise RuntimeError("STABILITY_API_KEY is not set")
    w, h = _img_size(size)
    payload = {
        "text_prompts": [{"text": prompt}],
        "cfg_scale": 7,
        "height": h,
        "width": w,
        "samples": 1,
        "steps": 30,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    r = requests.post(STABILITY_URL, headers=headers, data=json.dumps(payload), timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Stability error {r.status_code}: {r.text}")
    data = r.json()
    if not data.get("artifacts"):
        raise RuntimeError("Stability: empty artifacts")
    b64 = data["artifacts"][0].get("base64")
    if not b64:
        raise RuntimeError("Stability: no base64 in response")
    return base64.b64decode(b64)

def _openai_images_generate(prompt: str, size: str|None) -> bytes:
    key = os.getenv("OPENAI_IMAGES_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_IMAGES_API_KEY is not set")
    size = size or "1024x1024"
    client = OpenAI(api_key=key, base_url="https://api.openai.com/v1")
    img = client.images.generate(model="gpt-image-1", prompt=prompt, size=size, quality="high")
    b64 = img.data[0].b64_json
    return base64.b64decode(b64)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Команды:\n"
        "/imagine <описание> [--size 512|768|1024] — сгенерировать изображение\n"
        "Пришли фото с подписью — опишу картинку.\n"
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

def _parse_size_flag(text: str) -> tuple[str,str]:
    size = None
    prompt = text
    if "--size" in text:
        try:
            before, after = text.split("--size", 1)
            prompt = before.strip()
            size_token = after.strip().split()[0]
            if "512" in size_token: size = "512x512"
            elif "768" in size_token: size = "768x768"
            else: size = "1024x1024"
        except Exception:
            pass
    return prompt.strip(), size

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # мгновенный отклик
    notice = await update.message.reply_text("Генерирую изображение… ⏳")
    try:
        full = (update.message.text or "").strip()
        parts = full.split(" ", 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            await notice.edit_text("Напиши: /imagine рыжий кот на луне [--size 512|768|1024]")
            return
        prompt, size = _parse_size_flag(prompt)

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        img_bytes: bytes|None = None
        err_msgs = []

        # 1) Пытаемся через Stability (если есть ключ)
        if os.getenv("STABILITY_API_KEY"):
            try:
                img_bytes = _gen_via_stability(prompt, size)
            except Exception as e:
                err_msgs.append(f"Stability: {e}")

        # 2) Если не вышло — пробуем OpenAI (если есть ключ)
        if img_bytes is None and os.getenv("OPENAI_IMAGES_API_KEY"):
            try:
                img_bytes = _openai_images_generate(prompt, size)
            except Exception as e:
                err_msgs.append(f"OpenAI Images: {e}")

        if img_bytes is None:
            msg = "Не удалось сгенерировать изображение."
            if not os.getenv("STABILITY_API_KEY") and not os.getenv("OPENAI_IMAGES_API_KEY"):
                msg += "\nНет ключей: добавь STABILITY_API_KEY или OPENAI_IMAGES_API_KEY."
            else:
                msg += "\n" + "\n".join(err_msgs[:2])
            await notice.edit_text(msg)
            return

        await notice.delete()
        await update.message.reply_photo(photo=BytesIO(img_bytes), caption="Готово ✅")

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
