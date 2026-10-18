"""История — единый backend: FastAPI + aiogram 3 + Supabase."""

import os
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone, timedelta
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

# Московское время (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

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
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    
    v = v.astimezone(MSK_TZ)
    return v.strftime("%d.%m.%Y %H:%M")

def fetch_one(query) -> Optional[dict]:
    try:
        res = query.execute()
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"DB Fetch Error: {e}")
        return None

async def safe_answer(cq: CallbackQuery, text: str = None, show_alert: bool = False):
    try:
        await cq.answer(text, show_alert=show_alert)
    except Exception:
        pass

def validate_init_data(init_data: str) -> Optional[dict]:
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
        "id": user.id, "first_name": user.first_name,
        "last_name": user.last_name, "username": user.username,
        "status": status,
    }
    supabase.table("users").insert(new_user).execute()
    return new_user

def create_access_request(user_id: int) -> tuple[dict, bool]:
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
        "status": status, "resolved_at": now_utc().isoformat(), "resolved_by": admin_id,
    }).eq("id", request_id).eq("status", "pending").execute()
    supabase.table("users").update({
        "status": status, "updated_at": now_utc().isoformat(),
    }).eq("id", req["user_id"]).execute()
    return fetch_one(supabase.table("access_requests").select("*").eq("id", request_id))

def get_owner_by_connection(connection_id: str) -> Optional[int]:
    res = fetch_one(supabase.table("business_connections").select("owner_id").eq("id", connection_id))
    return res["owner_id"] if res else None

def upsert_chat(connection_id: str, chat: types.Chat, owner_id: int):
    supabase.table("chats").upsert({
        "connection_id": connection_id, "chat_id": chat.id, "chat_type": str(chat.type),
        "title": chat.title, "username": chat.username, "first_name": chat.first_name,
        "last_name": chat.last_name, "owner_id": owner_id,
    }, on_conflict="connection_id,chat_id").execute()

def upsert_contact(owner_id: int, chat: types.Chat):
    if str(chat.type) != "private":
        return
    supabase.table("contacts").upsert({
        "owner_id": owner_id, "telegram_user_id": chat.id, "first_name": chat.first_name,
        "last_name": chat.last_name, "username": chat.username,
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
        "Для сохранения сгорающих или однократных — ответьте на них любым сообщением.\n\n"
        "• Бот присылает уведомления, когда собеседник удаляет или редактирует сообщения.\n"
        "• Может сохранять сгорающие и однократные фото, голосовые и видео."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Скопировать @", copy_text=types.CopyTextButton(text=f"@{bot_username}")),
            InlineKeyboardButton(text="🔌 Подключить Историю", url="tg://settings/edit")
        ],
        [InlineKeyboardButton(text="? Инструкция", callback_data="show_instructions")]
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
        "Для начала нажмите кнопку ниже.", reply_markup=kb)

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
        f"📅 Дата: {now_utc().astimezone(MSK_TZ).strftime('%d.%m.%Y')}\n"
        f"🕒 Время: {now_utc().astimezone(MSK_TZ).strftime('%H:%M')}\n"
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
        "id": conn.id, "owner_id": conn.user.id,
        "is_enabled": conn.is_enabled, "updated_at": now_utc().isoformat(),
    }, on_conflict="id").execute()

    if conn.is_enabled:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть Историю", web_app=types.WebAppInfo(url=MINI_APP_URL))]
        ])
        text = (
            "✅ <b>Бот успешно подключён к автоматизации чатов!</b>\n\n"
            "Теперь я буду сохранять историю сообщений и присылать уведомления, "
            "если собеседник удалит или изменит сообщение.\n\n"
            "Все сохранённые данные доступны в приложении «История»."
        )
        try:
            await bot.send_message(conn.user.id, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Failed to send business connection notify: {e}")
    else:
        try:
            await bot.send_message(conn.user.id, "⚠️ <b>Бот отключён от автоматизации чатов.</b>\nУведомления об удалённых сообщениях больше не будут приходить.")
        except Exception as e:
            logger.error(f"Failed to send business disconnect notify: {e}")

@dp.business_message()
async def on_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
    upsert_chat(msg.business_connection_id, msg.chat, owner_id)
    upsert_contact(owner_id, msg.chat)

    messages_to_process = [msg]
    if msg.reply_to_message:
        messages_to_process.append(msg.reply_to_message)

    for target_msg in messages_to_process:
        sender = target_msg.from_user
        sender_id = sender.id if sender else None
        is_outgoing = (sender_id == owner_id)
        is_reply = (target_msg.message_id != msg.message_id)
        
        # Определяем тип медиа
        media_type = media_file_id = None
        if target_msg.photo:
            media_type, media_file_id = "photo", target_msg.photo[-1].file_id
        elif target_msg.video:
            media_type, media_file_id = "video", target_msg.video.file_id
        elif target_msg.animation:
            media_type, media_file_id = "animation", target_msg.animation.file_id
            
        # Флаги TTL и Spoiler
        is_spoiler = getattr(target_msg, 'has_media_spoiler', False)
        is_ttl = getattr(target_msg, 'ttl_seconds', None) is not None
        is_ephemeral = is_spoiler or is_ttl

        # ИСХОДЯЩИЕ МЕДИА ИГНОРИРУЕМ
        if is_outgoing and media_type:
            continue

        # Ищем запись в БД (если это ответ на сообщение)
        db_msg = None
        if is_reply:
            db_msg = fetch_one(supabase.table("messages").select("id, media_file_id").eq("connection_id", msg.business_connection_id).eq("chat_id", target_msg.chat.id).eq("message_id", target_msg.message_id))

        was_saved_ephemeral = False
        if db_msg and db_msg.get("media_file_id") == "EPHEMERAL":
            was_saved_ephemeral = True

        is_known_ephemeral = is_ephemeral or was_saved_ephemeral

        # ФИЛЬТРАЦИЯ
        if media_type and not is_known_ephemeral:
            # Если это медиа и мы не знаем, что оно исчезающее — игнорируем
            continue

        # СОХРАНЯЕМ В БД (все, что прошло фильтр)
        payload = {
            "connection_id": msg.business_connection_id, "chat_id": target_msg.chat.id,
            "message_id": target_msg.message_id, "owner_id": owner_id,
            "sender_id": sender_id,
            "sender_name": sender.full_name if sender else None,
            "sender_username": sender.username if sender else None,
            "text": target_msg.text or target_msg.caption,
            "media_type": media_type, 
            "media_file_id": "EPHEMERAL" if is_known_ephemeral else media_file_id,
            "sent_at": target_msg.date.isoformat() if target_msg.date else None,
        }
        supabase.table("messages").upsert(payload, on_conflict="connection_id,chat_id,message_id").execute()
        
        # Скачиваем медиа
        if media_file_id and media_type in ("photo", "video", "animation"):
            if is_known_ephemeral:
                try:
                    file = await bot.get_file(media_file_id)
                    if file.file_size and file.file_size <= 20 * 1024 * 1024:
                        buf = BytesIO()
                        await bot.download_file(file.file_path, buf)
                        file_bytes = buf.getvalue()
                        
                        path = f"{owner_id}/{msg.business_connection_id}/{target_msg.chat.id}/{target_msg.message_id}.{media_type}"
                        
                        supabase.storage.from_(STORAGE_BUCKET).upload(
                            path, file_bytes,
                            file_options={
                                "content-type": "image/jpeg" if media_type == "photo" else ("video/mp4" if media_type == "video" else "video/mp4"),
                                "upsert": "true"
                            }
                        )
                        mid_row = fetch_one(supabase.table("messages").select("id").eq("connection_id", msg.business_connection_id).eq("chat_id", target_msg.chat.id).eq("message_id", target_msg.message_id))
                        if mid_row:
                            supabase.table("media_files").upsert({
                                "message_id": mid_row["id"], "owner_id": owner_id,
                                "media_type": media_type, "storage_path": path, "file_size": file.file_size,
                            }, on_conflict="message_id").execute()

                        # Отправляем в чат, если это ответ на входящее
                        if is_reply and not is_outgoing:
                            try:
                                if media_type == "photo":
                                    prefix = "📸 Сохранено фото"
                                    if is_spoiler: prefix = "📸 Сохранено однократное фото"
                                    elif is_ttl: prefix = "📸 Сохранено исчезающее фото"
                                    input_file = types.BufferedInputFile(file_bytes, filename="photo.jpg")
                                    await bot.send_photo(owner_id, input_file, caption=prefix)
                                elif media_type == "video":
                                    prefix = "🎬 Сохранено видео"
                                    if is_spoiler: prefix = "🎬 Сохранено однократное видео"
                                    elif is_ttl: prefix = "🎬 Сохранено исчезающее видео"
                                    input_file = types.BufferedInputFile(file_bytes, filename="video.mp4")
                                    await bot.send_video(owner_id, input_file, caption=prefix)
                                elif media_type == "animation":
                                    input_file = types.BufferedInputFile(file_bytes, filename="animation.mp4")
                                    await bot.send_animation(owner_id, input_file, caption="🎬 Сохранена GIF-анимация")
                            except Exception as send_err:
                                logger.warning(f"Could not send TTL/Spoiler media back to owner: {send_err}")
                except Exception as e:
                    logger.warning(f"media save failed: {e}")

@dp.edited_business_message()
async def on_edited_business_message(msg: types.Message):
    if not msg.business_connection_id:
        return
    owner_id = get_owner_by_connection(msg.business_connection_id)
    if not owner_id:
        return
        
    row = fetch_one(supabase.table("messages").select("id,text,sender_id,sender_name,sender_username").eq("connection_id", msg.business_connection_id).eq("chat_id", msg.chat.id).eq("message_id", msg.message_id))
    if not row:
        return
        
    old_text, new_text = row.get("text"), (msg.text or msg.caption)
    if old_text == new_text:
        return
        
    supabase.table("message_versions").insert({
        "message_id": row["id"], "old_text": old_text,
        "new_text": new_text, "edited_at": now_utc().isoformat(),
    }).execute()
    
    supabase.table("messages").update({"text": new_text}).eq("id", row["id"]).execute()
    
    sender_id = row.get("sender_id")
    if sender_id != owner_id:
        sender_name = row.get("sender_name") or "Неизвестно"
        username = row.get("sender_username")
        uname_text = f"@{username}" if username else "Не указан"
        
        text = (
            f"✏️ <b>СООБЩЕНИЕ ИЗМЕНЕНО</b>\n\n"
            f"👤 Отправитель: {sender_name}\n"
            f"🔗 Username: {uname_text}\n"
            f"🆔 Telegram ID: <code>{sender_id}</code>\n\n"
            f"Было:\n"
            f"<blockquote>«{old_text or 'Содержимое недоступно'}»</blockquote>\n"
            f"Стало:\n"
            f"<blockquote>«{new_text or 'Содержимое недоступно'}»</blockquote>"
        )
        try:
            await bot.send_message(owner_id, text)
        except Exception as e:
            logger.warning(f"Could not send edit notify: {e}")

@dp.deleted_business_messages()
async def on_deleted_business_messages(event: types.BusinessMessagesDeleted):
    owner_id = get_owner_by_connection(event.business_connection_id)
    if not owner_id:
        return
        
    parts = []
    media_to_send = [] 
    valid_message_ids = []
    
    for mid in event.message_ids:
        row = fetch_one(supabase.table("messages").select("*").eq("connection_id", event.business_connection_id).eq("chat_id", event.chat.id).eq("message_id", mid))
        
        if row:
            m = row
            if m.get("sender_id") == owner_id:
                continue
                
            valid_message_ids.append(mid)
            
            sent = fmt_dt(m.get("sent_at"))
            username = m.get('sender_username')
            uname_text = f"@{username}" if username else "Не указан"
            sender_id = m.get('sender_id') or '—'
            sender_name = m.get('sender_name') or 'Неизвестно'
            
            parts.append(
                f"👤 Отправитель: {sender_name}\n"
                f"🔗 Username: {uname_text}\n"
                f"🆔 Telegram ID: <code>{sender_id}</code>\n"
                f"💬 Сообщение:\n"
                f"<blockquote>«{m.get('text') or 'Содержимое недоступно'}»</blockquote>\n"
                f"🕒 Отправлено: {sent}"
            )
            
            if m.get("media_type"):
                mf_row = fetch_one(supabase.table("media_files").select("*").eq("message_id", m["id"]))
                if mf_row:
                    media_to_send.append({
                        "type": mf_row["media_type"],
                        "path": mf_row["storage_path"]
                    })
                    
    if not valid_message_ids:
        return
        
    event_hash = hashlib.sha256(
        f"{event.business_connection_id}:{event.chat.id}:{','.join(map(str, sorted(valid_message_ids)))}".encode()
    ).hexdigest()
    existing = fetch_one(supabase.table("deletion_events").select("id").eq("event_hash", event_hash))
    if existing:
        return
        
    supabase.table("deletion_events").insert({
        "connection_id": event.business_connection_id, "chat_id": event.chat.id,
        "message_ids": valid_message_ids, "owner_id": owner_id,
        "detected_at": now_utc().isoformat(), "event_hash": event_hash,
    }).execute()
            
    if parts:
        text = (
            "🗑 <b>УДАЛЕНО СООБЩЕНИЕ</b>\n\n" + "\n\n".join(parts) +
            f"\n\n🗑 Удаление обнаружено: {fmt_dt(now_utc())}"
        )
        
        try:
            await bot.send_message(owner_id, text)
            
            for media in media_to_send:
                try:
                    res = supabase.storage.from_(STORAGE_BUCKET).download(media["path"])
                    input_file = types.BufferedInputFile(res, filename=f"deleted_{media['type']}.jpg")
                    if media["type"] == "photo":
                        await bot.send_photo(owner_id, input_file, caption="📸 Прикрепленное фото из удаленного сообщения")
                    elif media["type"] == "video":
                        await bot.send_video(owner_id, input_file, caption="🎬 Прикрепленное видео из удаленного сообщения")
                    elif media["type"] == "animation":
                        await bot.send_animation(owner_id, input_file, caption="🎬 Прикрепленная GIF из удаленного сообщения")
                except Exception as e:
                    logger.warning(f"Could not send deleted media: {e}")
                    
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
    
    user_msgs = supabase.table("messages").select("id, chat_id").eq("owner_id", uid).execute()
    user_msg_ids = [m["id"] for m in user_msgs.data] if user_msgs.data else []
    msg_to_chat = {m["id"]: m["chat_id"] for m in user_msgs.data} if user_msgs.data else {}
    
    active_chat_ids = set()
    
    dels = supabase.table("deletion_events").select("chat_id").eq("owner_id", uid).execute()
    if dels.data:
        for d in dels.data:
            active_chat_ids.add(d["chat_id"])
            
    media = supabase.table("media_files").select("message_id").eq("owner_id", uid).execute()
    if media.data:
        for m in media.data:
            chat_id = msg_to_chat.get(m["message_id"])
            if chat_id:
                active_chat_ids.add(chat_id)
                
    if user_msg_ids:
        vers_res = supabase.table("message_versions").select("message_id").in_("message_id", user_msg_ids).execute()
        if vers_res.data:
            for v in vers_res.data:
                chat_id = msg_to_chat.get(v["message_id"])
                if chat_id:
                    active_chat_ids.add(chat_id)
                    
    if not active_chat_ids:
        return {"contacts": []}
        
    active_ids_list = list(active_chat_ids)
    
    query = supabase.table("contacts").select("*").eq("owner_id", uid).in_("telegram_user_id", active_ids_list)
    if q:
        filters = f"first_name.ilike.%{q}%,last_name.ilike.%{q}%,username.ilike.%{q}%"
        if q.isdigit():
            filters += f",telegram_user_id.eq.{q}"
        query = query.or_(filters)
        
    return {"contacts": query.order("created_at", desc=True).limit(100).execute().data}

@app.get("/api/chat/{chat_id}/messages")
async def api_chat_messages(chat_id: int, user: dict = Depends(get_verified_user)):
    uid = user["id"]
    
    msgs_res = supabase.table("messages").select(
        "id,message_id,text,media_type,sent_at,sender_name,sender_username,sender_id"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("sent_at", desc=True).limit(200).execute()
    all_msgs = msgs_res.data if msgs_res.data else []
    
    versions = []
    if all_msgs:
        ids = [m["id"] for m in all_msgs]
        versions = supabase.table("message_versions").select("*").in_("message_id", ids).execute().data
        
    dels_res = supabase.table("deletion_events").select(
        "id,message_ids,detected_at"
    ).eq("owner_id", uid).eq("chat_id", chat_id).order("detected_at", desc=True).limit(100).execute()
    dels = dels_res.data if dels_res.data else []
    
    deleted_telegram_ids = set()
    for d in dels:
        deleted_telegram_ids.update(d["message_ids"])
        
    edited_db_ids = {v["message_id"] for v in versions}
    
    filtered_msgs = []
    for m in all_msgs:
        if m.get("sender_id") == uid:
            continue
            
        is_deleted = m["message_id"] in deleted_telegram_ids
        is_edited = m["id"] in edited_db_ids
        
        if is_deleted or is_edited:
            filtered_msgs.append(m)
    
    return {"messages": filtered_msgs, "versions": versions, "deletions": dels}

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
