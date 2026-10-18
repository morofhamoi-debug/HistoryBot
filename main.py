"""История — единый backend: FastAPI + aiogram 3 + Supabase."""

import os
import json
import hmac
import hashlib
import logging
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

# ── Конфигурация ─────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5208615220"))
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
MINI_APP_URL = os.environ["MINI_APP_URL"]
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "history-media")

# ── Инициализация ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("istoriya")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ── Утилиты ──────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def fmt_dt(v: Optional[datetime | str]) -> str:
    if not v:
        return "—"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return v.strftime("%d.%m.%Y %H:%M")

def fetch_one(query) -> Optional[dict]:
    """Безопасное извлечение одной записи из Supabase."""
    try:
        res = query.execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"DB Fetch Error: {e}")
        return None

async def safe_answer(cq: CallbackQuery, text: str = None, show_alert: bool = False):
    """Безопасный ответ на callback."""
    try:
        await cq.answer(text, show_alert=show_alert)
    except Exception:
        pass

def validate_init_data(init_data: str) -> Optional[dict]:
    """Проверка подписи Telegram WebApp initData."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed:
            return None
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        return json.loads(parsed["user"]) if "user" in parsed else None
    except Exception:
        return None

# ── FastAPI Зависимости ──────────────────────────────────────────────────────

async def get_verified_user(x_telegram_init_data: str = Header(None)) -> dict:
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="initData required")
    
    user = validate_init_data(x_telegram_init_data)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="invalid initData")
    
    db_user = fetch_one(supabase.table("users").select("status").eq("id", user["id"]))
    if not db_user or db_user.get("status") != "approved":
        raise HTTPException(status_code=403, detail="access denied")
    
    return user

# ── База данных ──────────────────────────────────────────────────────────────

def ensure_user(user: types.User) -> dict:
    existing = fetch_one(supabase.table("users").select("*").eq("id", user.id))
    if existing:
        return existing
    
    status = "approved" if user.id == ADMIN_ID else "pending"
    new_user = {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "username": user.username,
        "status": status,
    }
    supabase.table("users").insert(new_user).execute()
    return new_user

def create_access_request(user_id: int) -> tuple[dict, bool]:
    """Создает заявку. Возвращает (заявка, is_new)."""
    existing = fetch_one(supabase.table("access_requests").select("*").eq("user_id", user_id).eq("status", "pending"))
    if existing:
        return existing, False
    
    res = supabase.table("access_requests").insert({"user_id": user_id, "status": "pending"}).execute()
    return res.data[0], True

def update_request_status(request_id: int, status: str, admin_id: int) -> Optional[dict]:
    req = fetch_one(supabase.table("access_requests").select("*").eq("id", request_id))
    if not req or req["status"] != "pending":
        return None
    
    supabase.table("access_requests").update({
        "status": status, 
        "resolved_at": now_utc().isoformat(), 
        "resolved_by": admin_id,
    }).eq("id", request_id).eq("status", "pending").execute()
    
    supabase.table("users").update({
        "status": status, 
        "updated_at": now_utc().isoformat(),
    }).eq("id", req["user_id"]).execute()
    
    return fetch_one(supabase.table("access_requests").select("*").eq("id", request_id))

def get_owner_by_connection(connection_id: str) -> Optional[int]:
    res = fetch_one(supabase.table("business_connections").select("owner_id").eq("id", connection_id))
    return res["owner_id"] if res else None

def upsert_chat(connection_id: str, chat: types.Chat, owner_id: int):
    supabase.table("chats").upsert({
        "connection_id": connection_id,
        "chat_id": chat.id,
        "chat_type": str(chat.type),
        "title": chat.title,
        "username": chat.username,
        "first_name": chat.first_name,
        "last_name": chat.last_name,
        "owner_id": owner_id,
    }, on_conflict="connection_id,chat_id").execute()

def upsert_contact(owner_id: int, chat: types.Chat):
    if str(chat.type) != "private":
        return
    supabase.table("contacts").upsert({
        "owner_id": owner_id,
        "telegram_user_id": chat.id,
        "first_name": chat.first_name,
        "last_name": chat.last_name,
        "username": chat.username,
    }, on_conflict="owner_id,telegram_user_id").execute()

# ── Aiogram: Пользователи ────────────────────────────────────────────────────

async def get_instruction_msg():
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception:
        bot_username = "KhamoiHistory_Bot"

    text = (
        "✅ <b>Доступ открыт. Добро пожаловать в «Историю»!</b>\n\n"
        "<b>Бот не подключён</b>\n"
        "После подключения покажет удаленные и отредактированные сообщения.\n"
        "Для сохранения сгорающих — ответьте на него любым сообщением.\n\n"
        "• Бот присылает уведомления, когда собеседник удаляет или редактирует сообщения.\n"
        "• Может сохранять сгорающие фото, голосовые и видео."
    )
    
    # Кнопка copy_text доступна в aiogram 3.7+ (Bot API 7.0)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Скопировать @", 
                copy_text=types.CopyTextButton(text=f"@{bot_username}")
            ),
            InlineKeyboardButton(text="🔌 Подключить Историю", url="tg://settings/edit")
        ],
        [
            InlineKeyboardButton(text="? Инструкция", callback_data="show_instructions")
        ]
    ])
    return text, kb

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать пользоваться", callback_data="start_usage")]
    ])
    await message.answer(
        "👋 Добро пожаловать в «Историю»!\n"
        "🕰️ История — ваш помощник для сохранения доступной истории сообщений "
        "и отслеживания удалений.\n👨‍💻 Создатель: Khamoi\n\n"
        "Для начала нажмите кнопку ниже.", 
        reply_markup=kb
    )

@dp.callback_query(F.data == "start_usage")
async def cb_start_usage(cq: CallbackQuery):
    await safe_answer(cq)
    try:
        user = ensure_user(cq.from_user)
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка базы данных Supabase:</b>\n<code>{str(e)}</code>")
        return
        
    if user["status"] == "approved":
        text, kb = await get_instruction_msg()
        await cq.message.answer(text, reply_markup=kb)
        return
        
    if user["status"] in ("denied", "blocked"):
        await cq.message.answer("❌ Доступ запрещён.")
        return
        
    try:
        req, is_new = create_access_request(user["id"])
    except Exception as e:
        await cq.message.answer(f"⚠️ <b>Ошибка при создании заявки:</b>\n<code>{str(e)}</code>")
        return

    if not is_new:
        await cq.message.answer("⏳ Ваша заявка уже ожидает решения администратора.")
        return

    await cq.message.answer("📨 Заявка отправлена администратору. Ожидайте решения.")
    
    uname = f"@{cq.from_user.username}" if cq.from_user.username else "отсутствует"
    text = (
        f"🔔 <b>НОВАЯ ЗАЯВКА</b>\n"
        f"👤 Имя: {cq.from_user.first_name or '—'}\n"
        f"👥 Фамилия: {cq.from_user.last_name or 'отсутствует'}\n"
        f"🔗 Username: {uname}\n"
        f"🆔 Telegram ID: <code>{cq.from_user.id}</code>\n"
        f"📅 Дата: {now_utc().strftime('%d.%m.%Y')}\n"
        f"🕒 Время: {now_utc().strftime('%H:%M')}\n"
        "📌 Статус: Ожидает рассмотрения"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти в диалог", url=f"tg://user?id={cq.from_user.id}")],
        [InlineKeyboardButton(text="🟢 Разрешить", callback_data=f"approve:{req['id']}"),
         InlineKeyboardButton(text="🔴 Запретить", callback_data=f"deny:{req['id']}")],
    ])
    
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb)
    except Exception as e:
        logger.error("admin notify failed: %s", e)

@dp.callback_query(F.data == "show_instructions")
async def cb_show_instructions(cq: CallbackQuery):
    text = (
        "1. Нажмите кнопку \"Скопировать @\".\n"
        "2. Нажмите кнопку \"🔌 Подключить Историю\"\n"
        "3. Выберите \"Автоматизация чатов\" и вставьте скопированный юзернейм."
    )
    await safe_answer(cq, text, show_alert=True)

@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await safe_answer(cq, "Недостаточно прав", show_alert=True)
        return
        
    req_id = int(cq.data.split(":")[1])
    upd = update_request_status(req_id, "approved", cq.from_user.id)
    if not upd:
        await safe_answer(cq, "Заявка уже обработана", show_alert=True)
        return
        
    await safe_answer(cq, "Одобрено")
    
    text, kb = await get_instruction_msg()
    try:
        await bot.send_message(upd["user_id"], text, reply_markup=kb)
    except Exception as e:
        logger.error(f"failed to send instructions: {e}")
        
    await cq.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("deny:"))
async def cb_deny(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await safe_answer(cq, "Недостаточно прав", show_alert=True)
        return
        
    req_id = int(cq.data.split(":")[1])
    upd = update_request_status(req_id, "denied", cq.from_user.id)
    if not upd:
        await safe_answer(cq, "Заявка уже обработана", show_alert=True)
        return
        
    await safe_answer(cq, "Отклонено")
    try:
        await bot.send_message(upd["user_id"], "❌ Ваша заявка отклонена администратором.")
    except Exception:
        pass
    await cq.message.edit_reply_markup(reply_markup=None)

# ── Aiogram: Business-события ────────────────────────────────────────────────

@dp.business_connection()
async def on_business_connection(conn: types.BusinessConnection):
    supabase.table("business_connections").upsert({
        "id": conn.id,
        "owner_id": conn.user.id,
        "is_enabled": conn.is_enabled,
        "updated_at": now_utc().isoformat(),
    }, on_conflict="id").execute()

@dp.business_message()
async def on_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
        
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
        
    upsert_chat(msg.business_connection_id, msg.chat, owner_id)
    upsert_contact(owner_id, msg.chat)
    
    sender = msg.from_user
    media_type = media_file_id = None
    if msg.photo:
        media_type, media_file_id = "photo", msg.photo[-1].file_id
    elif msg.video:
        media_type, media_file_id = "video", msg.video.file_id
        
    supabase.table("messages").upsert({
        "connection_id": msg.business_connection_id,
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "owner_id": owner_id,
        "sender_id": sender.id if sender else None,
        "sender_name": sender.full_name if sender else None,
        "sender_username": sender.username if sender else None,
        "text": msg.text or msg.caption,
        "media_type": media_type,
        "media_file_id": media_file_id,
        "sent_at": msg.date.isoformat() if msg.date else None,
    }, on_conflict="connection_id,chat_id,message_id").execute()
    
    if media_file_id and media_type in ("photo", "video"):
        try:
            file = await bot.get_file(media_file_id)
            if file.file_size and file.file_size <= 20 * 1024 * 1024:
                buf = BytesIO()
                await bot.download_file(file.file_path, buf)
                path = f"{owner_id}/{msg.business_connection_id}/{msg.chat.id}/{msg.message_id}.{media_type}"
                
                supabase.storage.from_(STORAGE_BUCKET).upload(
                    path, buf.getvalue(),
                    file_options={
                        "content-type": "image/jpeg" if media_type == "photo" else "video/mp4",
                        "upsert": "true"
                    }
                )
                
                mid_row = fetch_one(supabase.table("messages").select("id").eq("connection_id", msg.business_connection_id).eq("chat_id", msg.chat.id).eq("message_id", msg.message_id))
                if mid_row:
                    supabase.table("media_files").upsert({
                        "message_id": mid_row["id"],
                        "owner_id": owner_id,
                        "media_type": media_type,
                        "storage_path": path,
                        "file_size": file.file_size,
                    }, on_conflict="message_id").execute()
        except Exception as e:
            logger.warning("media save failed: %s", e)

@dp.edited_business_message()
async def on_edited_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
        
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
        
    row = fetch_one(supabase.table("messages").select("id,text").eq("connection_id", msg.business_connection_id).eq("chat_id", msg.chat.id).eq("message_id", msg.message_id))
    if not row:
        return
        
    old_text, new_text = row.get("text"), (msg.text or msg.caption)
    if old_text == new_text:
        return
        
    supabase.table("message_versions").insert({
        "message_id": row["id"],
        "old_text": old_text,
        "new_text": new_text,
        "edited_at": now_utc().isoformat(),
    }).execute()
    
    supabase.table("messages").update({"text": new_text}).eq("id", row["id"]).execute()

@dp.deleted_business_messages()
async def on_deleted_business_messages(event: types.BusinessMessagesDeleted):
    owner_id = get_owner_by_connection(event.business_connection_id)
    if not owner_id:
        return
        
    event_hash = hashlib.sha256(
        f"{event.business_connection_id}:{event.chat.id}:{','.join(map(str, sorted(event.message_ids)))}".encode()
    ).hexdigest()
    
    existing = fetch_one(supabase.table("deletion_events").select("id").eq("event_hash", event_hash))
    if existing:
        return
        
    supabase.table("deletion_events").insert({
        "connection_id": event.business_connection_id,
        "chat_id": event.chat.id,
        "message_ids": list(event.message_ids),
        "owner_id": owner_id,
        "detected_at": now_utc().isoformat(),
        "event_hash": event_hash,
    }).execute()
    
    parts = []
    for mid in event.message_ids:
        row = fetch_one(supabase.table("messages").select("*").eq("connection_id", event.business_connection_id).eq("chat_id", event.chat.id).eq("message_id", mid))
        
        if row:
            m = row
            sent = fmt_dt(m.get("sent_at"))
            parts.append(
                f"👤 Отправитель: {m.get('sender_name') or 'Неизвестно'}\n"
                f"🔗 Username: {m.get('sender_username') or 'Не указан'}\n"
                f"🆔 Telegram ID: {m.get('sender_id') or '—'}\n"
                f"💬 Сообщение:\n«{m.get('text') or 'Содержимое недоступно'}»\n"
                f"🕒 Отправлено: {sent}"
            )
        else:
            parts.append(f"💬 Сообщение: «Содержимое недоступно»\n🆔 ID сообщения: {mid}")
            
    text = (
        "🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n\n" + "\n\n".join(parts) +
        f"\n\n🗑 Удаление обнаружено: {fmt_dt(now_utc())}"
    )
    try:
        await bot.send_message(owner_id, text)
    except Exception:
        pass

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(title="История API")

@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")
        
    try:
        data = await request.json()
        update = types.Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"WEBHOOK ERROR: {e}", exc_info=True)
        err_str = str(e)
        
        if "query is too old" not in err_str and "response timeout expired" not in err_str:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ <b>Ошибка в webhook:</b>\n<code>{err_str}</code>")
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
        if q.isdigit():
            filters += f",telegram_user_id.eq.{q}"
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
    if not mids:
        return {"media": []}
        
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
async def health():
    return {"status": "ok", "time": now_utc().isoformat()}

@app.get("/")
async def serve_index():
    return FileResponse("index.html", media_type="text/html")

@app.get("/static/app.js")
async def serve_js():
    return FileResponse("app.js", media_type="application/javascript")

@app.get("/static/style.css")
async def serve_css():
    return FileResponse("style.css", media_type="text/css")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
