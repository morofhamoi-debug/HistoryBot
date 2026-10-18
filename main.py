"""История — единый backend: FastAPI + aiogram 3 + Supabase."""

import os, json, hmac, hashlib, logging
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import parse_qsl
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException, Header, Depends, Query
from fastapi.responses import FileResponse
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from supabase import create_client, Client

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5208615220"))
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MINI_APP_URL = os.environ["MINI_APP_URL"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "history-media")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("istoriya")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def now_utc(): return datetime.now(timezone.utc)
def fmt_dt(v): return v.strftime("%d.%m.%Y %H:%M") if v else "—"

def validate_init_data(init_data: str) -> Optional[dict]:
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed: return None
        received = parsed.pop("hash")
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received): return None
        return json.loads(parsed["user"]) if "user" in parsed else None
    except Exception:
        return None

async def get_verified_user(x_telegram_init_data: str = Header(None)) -> dict:
    if not x_telegram_init_data:
        raise HTTPException(401, "initData required")
    user = validate_init_data(x_telegram_init_data)
    if not user or not user.get("id"):
        raise HTTPException(401, "invalid initData")
    res = supabase.table("users").select("status").eq("id", user["id"]).execute()
    if not res.data or res.data[0].get("status") != "approved":
        raise HTTPException(403, "access denied")
    return user

def ensure_user(user: types.User) -> dict:
    status = "approved" if user.id == ADMIN_ID else "pending"
    existing = supabase.table("users").select("*").eq("id", user.id).execute()
    if existing.data:
        return existing.data[0]
    
    supabase.table("users").insert({
        "id": user.id, "first_name": user.first_name,
        "last_name": user.last_name, "username": user.username, "status": status,
    }).execute()
    return supabase.table("users").select("*").eq("id", user.id).execute().data[0]

def create_access_request(user_id: int) -> dict:
    existing = supabase.table("access_requests").select("*").eq(
        "user_id", user_id).eq("status", "pending").execute()
    if existing.data: return existing.data[0]
    
    res = supabase.table("access_requests").insert({"user_id": user_id, "status": "pending"}).execute()
    return res.data[0]

def update_request_status(request_id: int, status: str, admin_id: int) -> Optional[dict]:
    req = supabase.table("access_requests").select("*").eq("id", request_id).execute()
    if not req.data or req.data[0]["status"] != "pending": return None
    
    supabase.table("access_requests").update({
        "status": status, "resolved_at": now_utc().isoformat(), "resolved_by": admin_id,
    }).eq("id", request_id).eq("status", "pending").execute()
    
    supabase.table("users").update({
        "status": status, "updated_at": now_utc().isoformat(),
    }).eq("id", req.data[0]["user_id"]).execute()
    
    return supabase.table("access_requests").select("*").eq("id", request_id).execute().data[0]

def get_owner_by_connection(connection_id: str) -> Optional[int]:
    res = supabase.table("business_connections").select("owner_id").eq("id", connection_id).execute()
    return res.data[0]["owner_id"] if res.data else None

def upsert_chat(connection_id: str, chat: types.Chat, owner_id: int):
    supabase.table("chats").upsert({
        "connection_id": connection_id, "chat_id": chat.id,
        "chat_type": str(chat.type), "title": chat.title, "username": chat.username,
        "first_name": chat.first_name, "last_name": chat.last_name, "owner_id": owner_id,
    }, on_conflict="connection_id,chat_id").execute()

def upsert_contact(owner_id: int, chat: types.Chat):
    if str(chat.type) != "private": return
    supabase.table("contacts").upsert({
        "owner_id": owner_id, "telegram_user_id": chat.id,
        "first_name": chat.first_name, "last_name": chat.last_name, "username": chat.username,
    }, on_conflict="owner_id,telegram_user_id").execute()

# ── Aiogram: /start и заявки ─────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать пользоваться", callback_data="start_usage")]
    ])
    await message.answer(
        "👋 Добро пожаловать в «Историю»!\n"
        "🕰️ История — ваш помощник для сохранения доступной истории сообщений "
        "и отслеживания удалений.\n👨‍💻 Создатель: Khamoi\n\n"
        "Для начала нажмите кнопку ниже.", reply_markup=kb)

@dp.callback_query(F.data == "start_usage")
async def cb_start_usage(cq: CallbackQuery):
    await cq.answer()
    try:
        user = ensure_user(cq.from_user)
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка базы данных Supabase:</b>\n<code>{str(e)}</code>")
        return
        
    if user["status"] == "approved":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📱 Открыть Историю", web_app=types.WebAppInfo(url=MINI_APP_URL))]])
        await cq.message.answer("✅ Доступ открыт. Добро пожаловать в «Историю»!", reply_markup=kb)
        return
    if user["status"] == "pending":
        await cq.message.answer("⏳ Ваша заявка уже ожидает решения администратора."); return
    if user["status"] in ("denied", "blocked"):
        await cq.message.answer("❌ Доступ запрещён."); return
        
    try:
        req = create_access_request(user["id"])
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка при созда.messageнии заявки:</b>\n<code>{str(e)}</code>")
        return

    await cq.message.answer("📨 Заявка отправлена администратору. Ожидайте решения.")
    uname = f"@{cq.from_user.username}" if cq.from_user.username else "отсутствует"
    text = (f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n"
            f"👤 Имя: {cq.from_user.first_name or '—'}\n"
            f"👥 Фамилия: {cq.from_user.last_name or 'отсутствует'}\n"
            f"🔗 Username: {uname}\n"
            f"🆔 Telegram ID: <code>{cq.from_user.id}</code>\n"
            f"📅 Дата: {now_utc().strftime('%d.%m.%Y')}\n"
            f"🕒 Время: {now_utc().strftime('%H:%M')}\n"
            "📌 Статус: Ожидает рассмотрения")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти в диалог", url=f"tg://user?id={cq.from_user.id}")],
        [InlineKeyboardButton(text="🟢 Разрешить", callback_data=f"approve:{req['id']}"),
         InlineKeyboardButton(text="🔴 Запретить", callback_data=f"deny:{req['id']}")],
    ])
    try: await bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except Exception as e: logger.error("admin notify failed: %s", e)

@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Недостаточно прав", show_alert=True); return
    req_id = int(cq.data.split(":")[1])
    upd = update_request_status(req_id, "approved", cq.from_user.id)
    if not upd:
        await cq.answer("Заявка уже обработана", show_alert=True); return
    await cq.answer("Одобрено")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 Открыть Историю", web_app=types.WebAppInfo(url=MINI_APP_URL))]])
    try: await bot.send_message(upd["user_id"], "✅ Ваша заявка одобрена!\nДобро пожаловать в «Историю»!", reply_markup=kb)
    except Exception: pass
    await cq.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("deny:"))
async def cb_deny(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("Недостаточно прав", show_alert=True); return
    req_id = int(cq.data.split(":")[1])
    upd = update_request_status(req_id, "denied", cq.from_user.id)
    if not upd:
        await cq.answer("Заявка уже обработана", show_alert=True); return
    await cq.answer("Отклонено")
    try: await bot.send_message(upd["user_id"], "❌ Ваша заявка отклонена администратором.")
    except Exception: pass
    await cq.message.edit_reply_markup(reply_markup=None)

# ── Aiogram: Business-события ────────────────────────────────────────────────
@dp.business_connection()
async def on_business_connection(conn: types.BusinessConnection):
    supabase.table("business_connections").upsert({
        "id": conn.id, "owner_id": conn.user.id,
        "is_enabled": conn.is_enabled, "updated_at": now_utc().isoformat(),
    }, on_conflict="id").execute()

@dp.business_message()
async def on_business_message(msg: types.Message):
    if not msg.business_connection_id: return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id: return
    upsert_chat(msg.business_connection_id, msg.chat, owner_id)
    upsert_contact(owner_id, msg.chat)
    sender = msg.from_user
    media_type = media_file_id = None
    if msg.photo: media_type, media_file_id = "photo", msg.photo[-1].file_id
    elif msg.video: media_type, media_file_id = "video", msg.video.file_id
    supabase.table("messages").upsert({
        "connection_id": msg.business_connection_id, "chat_id": msg.chat.id,
        "message_id": msg.message_id, "owner_id": owner_id,
        "sender_id": sender.id if sender else None,
        "sender_name": sender.full_name if sender else None,
        "sender_username": sender.username if sender else None,
        "text": msg.text or msg.caption,
        "media_type": media_type, "media_file_id": media_file_id,
        "sent_at": msg.date.isoformat() if msg.date else None,
    }, on_conflict="connection_id,chat_id,message_id").execute()
    if media_file_id and media_type in ("photo", "video"):
        try:
            file = await bot.get_file(media_file_id)
            if file.file_size and file.file_size <= 20 * 1024 * 1024:
                buf = BytesIO(); await bot.download_file(file.file_path, buf)
                path = f"{owner_id}/{msg.business_connection_id}/{msg.chat.id}/{msg.message_id}.{media_type}"
                supabase.storage.from_(STORAGE_BUCKET).upload(
                    path, buf.getvalue(),
                    file_options={"content-type": "image/jpeg" if media_type == "photo" else "video/mp4",
                                  "upsert": "true"})
                mid = supabase.table("messages").select("id").eq(
                    "connection_id", msg.business_connection_id).eq(
                    "chat_id", msg.chat.id).eq("message_id", msg.message_id).execute().data[0]["id"]
                supabase.table("media_files").upsert({
                    "message_id": mid, "owner_id": owner_id, "media_type": media_type,
                    "storage_path": path, "file_size": file.file_size,
                }, on_conflict="message_id").execute()
        except Exception as e:
            logger.warning("media save failed: %s", e)

@dp.edited_business_message()
async def on_edited_business_message(msg: types.Message):
    if not msg.business_connection_id: return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id: return
    row = supabase.table("messages").select("id,text").eq(
        "connection_id", msg.business_connection_id).eq(
        "chat_id", msg.chat.id).eq("message_id", msg.message_id).execute()
    if not row.data: return
    old_text, new_text = row.data[0].get("text"), (msg.text or msg.caption)
    if old_text == new_text: return
    supabase.table("message_versions").insert({
        "message_id": row.data[0]["id"], "old_text": old_text,
        "new_text": new_text, "edited_at": now_utc().isoformat(),
    }).execute()
    supabase.table("messages").update({"text": new_text}).eq("id", row.data[0]["id"]).execute()

@dp.deleted_business_messages()
async def on_deleted_business_messages(event: types.BusinessMessagesDeleted):
    owner_id = get_owner_by_connection(event.business_connection_id)
    if not owner_id: return
    event_hash = hashlib.sha256(
        f"{event.business_connection_id}:{event.chat.id}:{','.join(map(str, sorted(event.message_ids)))}".encode()
    ).hexdigest()
    existing = supabase.table("deletion_events").select("id").eq("event_hash", event_hash).execute()
    if existing.data: return
    supabase.table("deletion_events").insert({
        "connection_id": event.business_connection_id, "chat_id": event.chat.id,
        "message_ids": list(event.message_ids), "owner_id": owner_id,
        "detected_at": now_utc().isoformat(), "event_hash": event_hash,
    }).execute()
    parts = []
    for mid in event.message_ids:
        row = supabase.table("messages").select("*").eq(
            "connection_id", event.business_connection_id).eq(
            "chat_id", event.chat.id).eq("message_id", mid).execute()
        if row.data:
            m = row.data[0]
            sent = "—"
            if m.get("sent_at"):
                try: sent = fmt_dt(datetime.fromisoformat(m["sent_at"].replace("Z", "+00:00")))
                except Exception: sent = m["sent_at"]
            parts.append(f"👤 Отправитель: {m.get('sender_name') or 'Неизвестно'}\n"
                         f"🔗 Username: {m.get('sender_username') or 'Не указан'}\n"
                         f"🆔 Telegram ID: {m.get('sender_id') or '—'}\n"
                         f"💬 Сообщение:\n«{m.get('text') or 'Содержимое недоступно'}»\n"
                         f"🕒 Отправлено: {sent}")
        else:
            parts.append(f"💬 Сообщение: «Содержимое недоступно»\n🆔 ID сообщения: {mid}")
    text = ("🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n\n" + "\n\n".join(parts) +
            f"\n\n🗑 Удаление обнаружено: {fmt_dt(now_utc())}")
    try: await bot.send_message(owner_id, text)
    except Exception: pass

# ── FastAPI ──────────────────────────────────────────────────────────────────
app = FastAPI(title="История")

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET: raise HTTPException(403, "invalid secret")
    try:
        data = await request.json()
        update = types.Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"WEBHOOK ERROR: {e}", exc_info=True)
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ <b>Ошибка в webhook:</b>\n<code>{str(e)}</code>")
        except Exception:
            pass
    return {"ok": True}

@app.get("/api/me")
async def api_me(user: dict = Depends(get_verified_user)):
    return {"user": user}

@app.get("/api/stats")
async def api_stats(user: dict = Depends(get_verified_user)):
    uid = user["id"]
    deleted = supabase.table("deletion_events").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    media = supabase.table("media_files").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    chats = supabase.table("chats").select("id", count="exact").eq("owner_id", uid).execute().count or 0
    return {"deleted": deleted, "media": media, "chats": chats}

@app.get("/api/contacts")
async def api_contacts(q: str = Query(""), user: dict = Depends(get_verified_user)):
    uid = user["id"]
    query = supabase.table("contacts").select("*").eq("owner_id", uid)
    if q:
        filters = f"first_name.ilike.%{q}%,last_name.ilike.%{q}%,username.ilike.%{q}%"
        if q.isdigit(): filters += f",telegram_user_id.eq.{q}"
        query = query.or_(filters)
    return {"contacts": query.order("created_at", desc=True).limit(100).execute().data}

@app.get("/api/chat/{chat_id}/messages")
async def api_chat_messages(chat_id: int, user: dict = Depends(get_verified_user)):
    uid = user["id"]
    msgs = supabase.table("messages").select(
        "id,message_id,text,media_type,sent_at,sender_name,sender_username,sender_id"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("sent_at", desc=True).limit(200).execute()
    versions = []
    if msgs.data:
        ids = [m["id"] for m in msgs.data]
        versions = supabase.table("message_versions").select("*").in_("message_id", ids).execute().data
    dels = supabase.table("deletion_events").select(
        "id,message_ids,detected_at"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("detected_at", desc=True).limit(100).execute()
    return {"messages": msgs.data, "versions": versions, "deletions": dels.data}

@app.get("/api/chat/{chat_id}/media")
async def api_chat_media(chat_id: int, user: dict = Depends(get_verified_user)):
    uid = user["id"]
    mids = supabase.table("messages").select("id").eq("owner_id", uid).eq("chat_id", chat_id).execute().data
    if not mids: return {"media": []}
    ids = [m["id"] for m in mids]
    files = supabase.table("media_files").select("*").eq("owner_id", uid).in_("message_id", ids).order("created_at", desc=True).limit(100).execute().data
    result = []
    for f in files:
        try:
            signed = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(f["storage_path"], 3600)
            result.append({**f, "url": signed.get("signedURL") or signed.get("signedUrl")})
        except Exception:
            result.append({**f, "url": None})
    return {"media": result}

@app.delete("/api/history")
async def api_delete_history(user: dict = Depends(get_verified_user)):
    uid = user["id"]
    supabase.table("messages").delete().eq("owner_id", uid).execute()
    supabase.table("deletion_events").delete().eq("owner_id", uid).execute()
    supabase.table("chats").delete().eq("owner_id", uid).execute()
    supabase.table("contacts").delete().eq("owner_id", uid).execute()
    return {"ok": True}

@app.get("/health")
async def health(): return {"status": "ok", "time": now_utc().isoformat()}

@app.get("/")
async def serve_index(): return FileResponse("index.html", media_type="text/html")

@app.get("/static/app.js")
async def serve_js(): return FileResponse("app.js", media_type="application/javascript")

@app.get("/static/style.css")
async def serve_css(): return FileResponse("style.css", media_type="text/css")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
